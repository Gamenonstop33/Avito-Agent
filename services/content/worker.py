"""content: автопостинг в VK и Telegram с согласованием в отдельном сообществе VK («GameNonStop рассылка»).

Источники VK (стены) публикуются только на стену VK, источники Telegram (каналы) — только в Telegram-канал.
Раз в 15 минут читает источники, новые посты пересказывает через LLM и отправляет карточку участникам
сообщества согласования: «Опубликовать / Пропустить», без ответа пост сгорает через 8 часов. Если согласование
выключено кнопкой — публикует сам. Посты с видео и репосты не берём. Реклама не отсекается, а помечается.
Запуск: python -m services.content.worker
"""
import asyncio
import json
import logging
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import httpx

from services.content import logic, tg, vk_wall
from services.content.vk_token import AUTH, CODE_AUTH, check, exchange, parse_code, parse_url
from shared.config import env, env_list
from shared.db import connect, single_instance
from shared.llm import LLM
from shared.vk import VK, VKError, payload_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("content")
SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
TZ = timezone(timedelta(hours=int(env("OWNER_UTC_OFFSET", "3"))))  # время владельца, Москва по умолчанию
DAY_END = int(env("OWNER_DAY_END", "20"))
HELP = ("Здесь согласуются посты для стены ВК и Telegram-канала. Под каждой карточкой — «Опубликовать» или «Пропустить»; "
        f"без ответа пост сгорает через {logic.EXPIRE_HOURS} ч.\n"
        "Кнопки внизу включают и выключают согласование: когда оно выключено, посты публикуются сами.")


class Content:
    def __init__(self, db, llm: LLM, reader: VK, bot: VK, poster: VK, group_id: int, http: httpx.AsyncClient):
        self.db, self.llm, self.reader, self.bot, self.poster = db, llm, reader, bot, poster
        self.group_id, self.http = group_id, http
        self.album_id = int(env("VK_ALBUM_ID") or 0)  # альбом сообщества для фото постов (см. vk_wall)
        self.callback = False  # callback-кнопки: нажатие не пишет сообщение в чат (нужно событие message_event)
        self.wait: dict[int, str] = {}  # кто сейчас должен прислать ссылку источника: vk_id → src_add | src_del
        # Какие площадки ведёт этот экземпляр. ВК и Telegram можно развести по разным серверам (у каждого своя БД):
        # оба слушают один чат согласования, но каждый действует только по своей площадке. Общие ответы
        # (приветствие, подсказки, справка) даёт только «главный» — тот, что ведёт ВК.
        self.targets = tuple(t for t in ("vk", "tg") if t in (env_list("CONTENT_TARGETS") or ["vk", "tg"]))
        self.primary = "vk" in self.targets
        self.solo = len(self.targets) == 2

    async def tell(self, peer: int, text: str, kb: dict | None = None) -> None:
        """Служебный ответ бота: через TEMP_MINUTES сам исчезнет из чата."""
        cmid = await self.bot.send(peer, text, kb)
        if cmid:
            await self.db.execute("INSERT INTO content.temp_msgs (vk_id, cmid, delete_at) VALUES ($1,$2, now() + "
                                  f"interval '{logic.TEMP_MINUTES} minutes') ON CONFLICT DO NOTHING", peer, cmid)

    async def sweep(self) -> None:
        """Убирает из чатов служебные ответы, у которых вышло время."""
        due: dict[int, list[str]] = {}
        for r in await self.db.fetch("DELETE FROM content.temp_msgs WHERE delete_at <= now() RETURNING vk_id, cmid"):
            due.setdefault(r["vk_id"], []).append(str(r["cmid"]))
        for peer, cmids in due.items():
            try:
                await self.bot.call("messages.delete", peer_id=peer, cmids=",".join(cmids), delete_for_all=1)
            except Exception as e:
                log.warning("служебные сообщения у %s не удалились: %s", peer, e)

    async def flag(self, key: str) -> bool:
        return await self.db.fetchval("SELECT value FROM content.settings WHERE key=$1", key) != "0"

    async def get(self, key: str) -> str | None:
        return await self.db.fetchval("SELECT value FROM content.settings WHERE key=$1", key)

    async def put(self, key: str, value: str) -> None:
        await self.db.execute("INSERT INTO content.settings (key, value) VALUES ($1,$2) "
                              "ON CONFLICT (key) DO UPDATE SET value=$2", key, value)

    # ── токен пользователя VK (фото в альбом сообщества) ──

    async def user_token(self) -> str:
        """Суточный токен из БД: его присылают в чат согласования по утреннему напоминанию (remind_token)."""
        token, exp = await self.get("vk_user_token"), float(await self.get("vk_user_token_exp") or 0)
        if not token or (exp and exp - time.time() < 60):
            raise logic.TokenNeeded("токен VK истёк — обновите его по ссылке из напоминания")
        return token

    async def remind_token(self) -> None:
        """Утром (logic.TOKEN_HOUR) просит в чате согласования обновить токен; не обновили — повторяет."""
        token, exp = await self.get("vk_user_token"), float(await self.get("vk_user_token_exp") or 0)
        if "vk" not in self.targets or (token and not exp):  # бессрочный токен напоминаний не требует
            return
        now = datetime.now(TZ)
        if not logic.token_reminder_due(now, exp if token else 0, float(await self.get("token_reminded") or 0),
                                        DAY_END):
            return
        await self.put("token_reminded", str(now.timestamp()))
        link = CODE_AUTH if env("VK_APP_SECRET") else AUTH
        await self.announce(logic.token_request(link, exp if token else 0, now, env("VK_USER_ID")))

    async def take_token(self, text: str) -> str:
        """Адрес страницы после «Разрешить», присланный в чат → токен в БД, очередь постов — сразу в работу.
        В адресе либо код (сервер сам получает по нему токен — так он выдан на IP сервера), либо готовый токен."""
        code = parse_code(text)
        try:
            got = await exchange(code) if code else parse_url(text)
        except VKError as e:
            return f"{e}. Код одноразовый и живёт около часа — откройте ссылку из напоминания ещё раз."
        if not got:
            return "Не нашёл код — пришлите адрес открывшейся страницы целиком."
        token, ttl, uid = got
        need = int(env("VK_USER_ID") or 0)
        if need and uid != need:
            return (f"Это токен другого аккаунта (vk.com/id{uid}). Откройте ссылку там, где в VK выполнен вход "
                    f"под vk.com/id{need}, и пришлите адрес ещё раз.")
        err = await check(token)
        if err:
            return f"Токен не прошёл проверку: {err}"
        exp = time.time() + ttl if ttl else 0
        await self.put("vk_user_token", token)
        await self.put("vk_user_token_exp", str(exp))
        log.info("токен VK обновлён (vk.com/id%s)", uid)
        asyncio.create_task(self.retry_safe())
        until = f"до {datetime.fromtimestamp(exp, TZ):%d.%m %H:%M} МСК" if exp else "бессрочно"
        return f"✅ Токен VK обновлён, действует {until}. Посты из очереди публикую."

    async def retry_safe(self) -> None:
        try:
            await self.retry_queued()
        except Exception:
            log.exception("повтор очереди не удался")

    async def menu(self) -> dict:
        return logic.menu(await self.flag("approve_vk"), await self.flag("approve_tg"))

    # ── источники ──

    async def poll_once(self) -> None:
        try:
            await self.remind_token()
        except Exception:
            log.exception("напоминание о токене не отправлено")
        await self.retry_safe()
        known = {r["source_key"] for r in await self.db.fetch("SELECT source_key FROM content.posts")}
        sources = await self.db.fetch("SELECT target, name FROM content.sources WHERE target = ANY($1) "
                                      "ORDER BY (target = 'tg'), added_at",  # порядок: сначала ВК, потом Telegram
                                      list(self.targets))
        for target, name in ((s["target"], s["name"]) for s in sources):
            try:
                items = ((await self.reader.call("wall.get", count=15, **logic.wall_args(name)))["items"]
                         if target == "vk" else await tg.fetch_channel(self.http, name))
                for p in logic.candidates(items, await self.since(f"{target}:{name}"), known):
                    await self.take(f"{target}:{name}", target, p)
            except Exception:
                log.exception("источник %s:%s не обработан", target, name)
        gone = await self.db.fetch(
            "UPDATE content.posts SET status='expired', decided_at=now() WHERE status='pending' AND target = ANY($1) "
            f"AND created_at < now() - interval '{logic.EXPIRE_HOURS} hours' RETURNING id", list(self.targets))
        for r in gone:
            log.info("пост №%s сгорел без ответа", r["id"])
            await self.close_cards(r["id"])

    async def since(self, source: str) -> float:
        """С какого момента берём посты источника: с его первого появления (старые посты не трогаем)."""
        v = await self.db.fetchval("SELECT value FROM content.settings WHERE key=$1", f"since:{source}")
        if v is None:  # источники VK, работавшие до появления этой настройки, — с первого запуска сервиса
            v = (await self.db.fetchval("SELECT value FROM content.settings WHERE key='started_at'")
                 if source.startswith("vk:") else str(time.time()))
            await self.db.execute("INSERT INTO content.settings (key, value) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                                  f"since:{source}", v)
        return float(v)

    async def take(self, source: str, target: str, p: dict) -> None:
        key, title = f"{p['owner_id']}_{p['id']}", (p.get("text") or "").split("\n")[0][:120]
        where = "Telegram-канала" if target == "tg" else "сообщества ВКонтакте"
        r = {"skip": logic.skip_reason(p)} if logic.skip_reason(p) else await vk_wall.rewrite(self.llm, p, where)
        row = await self.db.fetchrow(
            "INSERT INTO content.posts (source, source_key, source_date, title, text, ad, photos, status, note, target) "
            "VALUES ($1,$2,to_timestamp($3),$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (source_key) DO NOTHING RETURNING *",
            source, key, p["date"], title, r.get("text"), r.get("ad", ""), json.dumps(vk_wall.photo_urls(p)),
            "skipped" if "skip" in r else "pending", r.get("skip"), target)
        if not row or "skip" in r:
            log.info("%s пропущен: %s", key, r.get("skip") or "уже есть")
            return
        log.info("%s → пост №%s%s", key, row["id"], " (возможна реклама)" if row["ad"] else "")
        if await self.flag(f"approve_{target}"):
            return await self.send_card(dict(row))
        err = await self.publish(row["id"], "auto")
        if err and await self.db.fetchval("SELECT status FROM content.posts WHERE id=$1", row["id"]) == "queued":
            await self.announce(err)
        elif err:  # сам опубликовать не смог — отдаём людям
            await self.send_card(dict(row), note=f"Сам опубликовать не смог: {err}")

    async def announce(self, text: str) -> None:
        """Сообщение всем согласующим (не служебное — само не удаляется)."""
        for r in await self.db.fetch("SELECT vk_id FROM content.reviewers"):
            try:
                await self.bot.send(r["vk_id"], text)
            except Exception as e:
                log.warning("сообщение для %s не дошло: %s", r["vk_id"], e)

    async def retry_queued(self) -> None:
        """Повтор одобренных постов из очереди; VK всё ещё отказывает — остальные не трогаем до следующего круга."""
        for r in await self.db.fetch("SELECT * FROM content.posts WHERE status='queued' AND target = ANY($1) "
                                     "ORDER BY id", list(self.targets)):
            err = await self.publish(r["id"], r["decided_by"] or "auto")
            st = await self.db.fetchval("SELECT status FROM content.posts WHERE id=$1", r["id"])
            if st == "published":
                out = await self.db.fetchval("SELECT vk_post_id FROM content.posts WHERE id=$1", r["id"])
                await self.announce(f"✅ Пост №{r['id']} из очереди опубликован: {self.link(r['target'], out)}")
            elif st == "queued":
                break
            elif err:  # сбой уже не временный — решение снова за людьми
                await self.send_card(dict(r), note=err)
        gone = await self.db.fetch(
            "UPDATE content.posts SET status='expired', decided_at=now() WHERE status='queued' AND target = ANY($1) "
            f"AND decided_at < now() - interval '{logic.QUEUE_HOURS} hours' RETURNING id, note", list(self.targets))
        for r in gone:
            log.info("пост №%s снят из очереди", r["id"])
            await self.announce(f"Пост №{r['id']} не вышел за {logic.QUEUE_HOURS} ч и снят из очереди. "
                                f"Последняя ошибка: {r['note']}")

    # ── согласование ──

    async def send_card(self, row: dict, to: list[int] | None = None, note: str = "") -> None:
        if not row["card_att"] and json.loads(row["photos"]):
            try:
                row["card_att"] = ",".join([await vk_wall.upload_message(self.bot, self.http, u)
                                            for u in json.loads(row["photos"])])
                await self.db.execute("UPDATE content.posts SET card_att=$2 WHERE id=$1", row["id"], row["card_att"])
            except Exception as e:
                log.warning("фото к карточке №%s не загрузились: %s", row["id"], e)
                note = (note + " " if note else "") + "Фото к карточке не приложились, в посте они будут."
        to = to or [r["vk_id"] for r in await self.db.fetch("SELECT vk_id FROM content.reviewers")]
        if not to:
            log.warning("пост №%s: согласовать некому — никто из участников не написал сообществу", row["id"])
        for uid in to:
            try:
                cmid = await self.bot.send(uid, logic.card(row, note),
                                           logic.card_keyboard(row["id"], self.callback, row["target"]),
                                           row["card_att"] or None)
                if cmid:
                    await self.db.execute("INSERT INTO content.cards (post_id, vk_id, cmid) VALUES ($1,$2,$3) "
                                          "ON CONFLICT DO NOTHING", row["id"], uid, cmid)
            except Exception as e:
                log.warning("карточка №%s не дошла до %s: %s", row["id"], uid, e)

    async def publish(self, post_id: int, who: str) -> str | None:
        """Публикует пост, если он ещё ждёт (или стоит в очереди). Возвращает None или текст для людей.

        Временный сбой VK/сети — пост встаёт в очередь (status='queued'), повторяет retry_queued."""
        row = await self.db.fetchrow("UPDATE content.posts SET status='publishing' WHERE id=$1 "
                                     "AND status IN ('pending','queued') RETURNING *", post_id)
        if not row:
            st = await self.db.fetchval("SELECT status FROM content.posts WHERE id=$1", post_id)
            await self.close_cards(post_id)
            return f"Пост №{post_id} уже {logic.STATUS.get(st, 'не найден')}"
        user = None
        try:
            urls = json.loads(row["photos"])
            if row["target"] == "tg":
                vk_id = await tg.publish(self.http, env("TG_BOT_TOKEN"), env("TG_CHANNEL_ID"), row["text"], urls)
            else:
                user = VK(await self.user_token(), 0)
                poster = user if env("VK_USER_POSTS") == "1" else self.poster  # кто создаёт запись: см. vk_wall
                vk_id = await vk_wall.publish(poster, user, self.http, self.group_id, row["text"], urls,
                                              self.album_id)
        except Exception as e:
            if isinstance(e, VKError) and e.code == 5 and user and getattr(await check(user.token), "code", 0) == 5:
                await self.db.execute("DELETE FROM content.settings WHERE key='vk_user_token'")  # отозван раньше срока
                e = logic.TokenNeeded(f"токен VK недействителен ({e}) — обновите его по ссылке из напоминания")
            if logic.retryable(e):
                log.warning("пост №%s в очереди: %s", post_id, e)
                await self.db.execute("UPDATE content.posts SET status='queued', note=$2, decided_by=COALESCE("
                                      "decided_by, $3), decided_at=COALESCE(decided_at, now()) WHERE id=$1",
                                      post_id, str(e)[:500], who)
                await self.close_cards(post_id)
                return logic.queued_text(post_id, str(e)[:300])
            log.exception("пост №%s не опубликован", post_id)
            await self.db.execute("UPDATE content.posts SET status='pending', note=$2 WHERE id=$1", post_id, str(e)[:500])
            return f"😬 Пост №{post_id} не опубликован: {str(e)[:400]}"
        finally:
            if user:
                await user.close()
        await self.db.execute("UPDATE content.posts SET status='published', decided_by=$2, vk_post_id=$3, "
                              "decided_at=now(), note=NULL WHERE id=$1", post_id, who, vk_id)
        log.info("пост №%s опубликован (%s): %s", post_id, who, self.link(row["target"], vk_id))
        await self.close_cards(post_id)
        return None

    def link(self, target: str, out_id: int) -> str:
        if target == "tg":
            ch = env("TG_CHANNEL_ID")
            return f"https://t.me/{ch[1:]}/{out_id}" if ch.startswith("@") else f"Telegram-канал, сообщение {out_id}"
        return f"https://vk.com/wall-{self.group_id}_{out_id}"

    async def close_cards(self, post_id: int, uid: int | None = None) -> None:
        """Пост решён — убираем его карточки из чатов согласующих, чтобы не висели (uid — только у одного)."""
        for r in await self.db.fetch("DELETE FROM content.cards WHERE post_id=$1 AND ($2::bigint IS NULL OR vk_id=$2) "
                                     "RETURNING vk_id, cmid", post_id, uid):
            try:
                await self.bot.call("messages.delete", peer_id=r["vk_id"], cmids=r["cmid"], delete_for_all=1)
            except Exception as e:
                log.warning("карточка №%s у %s не удалилась: %s", post_id, r["vk_id"], e)

    async def show_sources(self, peer: int) -> None:
        rows = await self.db.fetch("SELECT target, name, title FROM content.sources ORDER BY (target='tg'), added_at")
        await self.tell(peer, logic.sources_text([dict(r) for r in rows], self.targets),
                        logic.sources_keyboard() if self.primary else None)  # кнопки — один раз, от главного

    async def edit_source(self, mode: str, text: str, uid: int) -> str:
        """Добавляет или убирает источник по присланной ссылке. Возвращает ответ пользователю."""
        src = logic.parse_source(text)
        if not src:
            return "Это не похоже на ссылку источника. Ничего не изменил.\n\n" + logic.LINK_FORMAT
        target, name = src
        url = logic.source_url(target, name)
        if mode == "src_del":
            gone = await self.db.fetchval("DELETE FROM content.sources WHERE target=$1 AND lower(name)=lower($2) "
                                          "RETURNING name", target, name)
            return f"🗑 Убрал {url} — новые посты оттуда больше не берём." if gone else f"{url} в списке нет."
        if await self.db.fetchval("SELECT 1 FROM content.sources WHERE target=$1 AND lower(name)=lower($2)", target, name):
            return f"{url} уже в списке."
        title = ""
        try:  # проверяем, что источник открыт и читается
            if target == "vk":
                await self.reader.call("wall.get", count=1, **logic.wall_args(name))
                g = await self.reader.call("groups.getById", group_id=name)
                title = (g.get("groups") or [{}])[0].get("name", "")
            elif not await tg.fetch_channel(self.http, name):
                raise RuntimeError("в веб-версии канала нет постов (канал закрыт или просмотр отключён)")
        except Exception as e:
            log.info("источник %s не читается: %s", url, e)
            return (f"Не смог прочитать {url} — такого сообщества или канала нет, либо он закрыт. "
                    "Источник не добавлен.\n\n" + logic.LINK_FORMAT)
        await self.db.execute("INSERT INTO content.sources (target, name, title, added_by) VALUES ($1,$2,$3,$4)",
                              target, name, title, f"vk:{uid}")
        await self.db.execute("INSERT INTO content.settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE "
                              "SET value=$2", f"since:{target}:{name}", str(time.time()))  # только новые посты
        where = "в Telegram-канал" if target == "tg" else "на стену ВК"
        return (f"✅ Добавил {url}" + (f" — {title}" if title else "") + f".\nНовые посты оттуда пойдут {where}; "
                "те, что уже вышли, не трогаю.")

    async def on_update(self, upd: dict) -> None:
        """Сообщение сообществу или нажатие callback-кнопки под карточкой."""
        ev = upd.get("event")
        uid, peer = (ev["user_id"], ev["peer_id"]) if ev else (upd["from_id"], upd["peer_id"])
        cmd = ((ev.get("payload") or {}).get("cmd") if ev else payload_cmd(upd)) or ""

        async def say(text: str) -> None:
            await self.tell(peer, text, await self.menu())

        async def toast(text: str) -> None:
            """Ответ на нажатие: всплывающая подсказка; у обычных кнопок — сообщение."""
            if not ev:
                return await say(text)
            await self.bot.call("messages.sendMessageEventAnswer", event_id=ev["event_id"], user_id=uid, peer_id=peer,
                                event_data=json.dumps({"type": "show_snackbar", "text": text[:90]}))

        if not await self.db.fetchval("SELECT 1 FROM content.reviewers WHERE vk_id=$1", uid):
            if not await self.bot.call("groups.isMember", group_id=self.bot.group_id, user_id=uid):
                if self.primary:
                    await self.tell(peer, "Согласование доступно участникам сообщества. "
                                          "Вступите в него и напишите ещё раз.")
                return
            await self.db.execute("INSERT INTO content.reviewers (vk_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
            if self.primary:
                await say("Вы добавлены в согласующие 👌\n\n" + HELP)
            if not cmd:
                return
        text = upd.get("text") or ""
        if not cmd and not ev and "blank.html" in text and ("code=" in text or "access_token=" in text):  # токен VK
            if "vk" in self.targets:
                await say(await self.take_token(upd["text"]))
            return
        mode = self.wait.pop(uid, None)  # любая кнопка отменяет ожидание ссылки
        if mode and not cmd and not ev:
            src = logic.parse_source(upd.get("text") or "")
            if src and src[0] not in self.targets:
                return  # ссылка другой площадки — ответит экземпляр, который её ведёт
            if not src and not self.primary:
                return
            await say(await self.edit_source(mode, upd.get("text") or "", uid))
            return await self.show_sources(peer)
        if cmd == "sources":
            return await self.show_sources(peer)
        if cmd in ("src_add", "src_del"):
            self.wait[uid] = cmd
            if not self.primary:
                return
            await toast("Жду ссылку")
            return await self.tell(peer, ("Пришлите ссылку на источник, который добавить." if cmd == "src_add" else
                                          "Пришлите ссылку на источник, который убрать.") + "\n\n" + logic.LINK_FORMAT)
        if cmd in ("toggle_vk", "toggle_tg"):
            key, name, owner = ("approve_vk", "ВК", "vk") if cmd == "toggle_vk" else ("approve_tg", "ТГ", "tg")
            on = not await self.flag(key)  # переключают все экземпляры (меню рисует главный), отвечает ведущий площадку
            await self.db.execute("INSERT INTO content.settings (key, value) VALUES ($1,$2) "
                                  "ON CONFLICT (key) DO UPDATE SET value=$2", key, "1" if on else "0")
            if owner not in self.targets:
                return
            return await say(f"{name}: согласование включено — посты ждут Вашего решения." if on else
                             f"{name}: согласование выключено — новые посты публикуются сами.")
        if cmd == "pending":
            rows = await self.db.fetch("SELECT * FROM content.posts WHERE status='pending' AND target = ANY($1) "
                                       "ORDER BY (target = 'tg'), id", list(self.targets))  # сначала ВК, потом Telegram
            for row in rows:
                await self.close_cards(row["id"], uid)  # прежнюю карточку этого поста убираем — без дублей
                await self.send_card(dict(row), to=[uid])
            who = "" if self.solo else f" ({logic.PLATFORM[self.targets[0]]})"
            queued = await self.db.fetchval("SELECT count(*) FROM content.posts WHERE status='queued' "
                                            "AND target = ANY($1)", list(self.targets))
            return await say((f"Ждут согласования{who}: {len(rows)}." if rows or not self.solo else
                              "Сейчас согласовывать нечего 👌") + (f"\nВ очереди на публикацию: {queued}." if queued else ""))
        decision = logic.parse_decision(cmd)
        if decision:
            kind, target, post_id = decision
            if target not in self.targets and not (target is None and self.solo):
                return  # пост другой площадки (или старая карточка без площадки) — не наш
            if kind == "pub":
                await toast("Публикую…")
                err = await self.publish(post_id, f"vk:{uid}")
                if err:
                    return await say(err)
                done = await self.db.fetchrow("SELECT target, vk_post_id FROM content.posts WHERE id=$1", post_id)
                return await say(f"✅ Пост №{post_id} опубликован: {self.link(done['target'], done['vk_post_id'])}")
            ok = await self.db.fetchval("UPDATE content.posts SET status='skipped', note='отклонён', decided_by=$2, "
                                        "decided_at=now() WHERE id=$1 AND status='pending' RETURNING id",
                                        post_id, f"vk:{uid}")
            st = await self.db.fetchval("SELECT status FROM content.posts WHERE id=$1", post_id)
            await self.close_cards(post_id)
            return await toast(f"Пост №{post_id} пропущен" if ok else
                               f"Пост №{post_id} уже {logic.STATUS.get(st, 'не найден')}")
        if self.primary and (cmd or (not ev and upd.get("text"))):
            await say(HELP)


async def sweep_loop(c: Content) -> None:
    while True:
        try:
            await c.sweep()
        except Exception:
            log.exception("уборка служебных сообщений упала")
        await asyncio.sleep(30)


async def poll_loop(c: Content) -> None:
    while True:
        try:
            await c.poll_once()
        except Exception:
            log.exception("опрос источников упал, повтор через %s с", logic.POLL_SECONDS)
        await asyncio.sleep(logic.POLL_SECONDS)


async def listen_loop(c: Content) -> None:
    while True:  # любая неожиданная ошибка — переподключаемся, а не падаем
        try:
            async for upd in c.bot.listen(with_events=True):
                try:
                    await c.on_update(upd)
                except Exception:
                    log.exception("ошибка обработки сообщения")
        except Exception:
            log.exception("VK: цикл упал, перезапуск через 5 с")
            await asyncio.sleep(5)


async def main() -> None:
    db = await connect(SCHEMA)
    await single_instance(db, "content")
    # первый запуск: берём только посты, вышедшие после него; согласование по умолчанию включено
    await db.execute("INSERT INTO content.settings (key, value) VALUES ('started_at',$1),('approve_vk','1'),"
                     "('approve_tg','1') ON CONFLICT DO NOTHING", str(time.time()))
    await db.execute("UPDATE content.posts SET status = CASE WHEN decided_by IS NULL THEN 'pending' ELSE 'queued' END "
                     "WHERE status='publishing'")  # оборвались на публикации; из очереди — обратно в очередь
    if not await db.fetchval("SELECT 1 FROM content.sources LIMIT 1"):  # первый запуск: список из .env, дальше — в БД
        mine = env_list("CONTENT_TARGETS") or ["vk", "tg"]
        for target, names in (("vk", env_list("CONTENT_VK_SOURCES") or ["xbox1store"]), ("tg", env_list("CONTENT_TG_SOURCES"))):
            for name in names if target in mine else []:
                await db.execute("INSERT INTO content.sources (target, name, added_by) VALUES ($1,$2,'env') "
                                 "ON CONFLICT DO NOTHING", target, name)
    reader, poster = VK(env("VK_SERVICE_KEY"), 0), VK(env("VK_POST_TOKEN"), 0)
    bot = VK(env("VK_CONTENT_GROUP_TOKEN"), int(env("VK_CONTENT_GROUP_ID")))
    http = httpx.AsyncClient(timeout=60, verify=ssl.create_default_context(cafile=certifi.where()))
    llm = LLM()
    try:
        vk_side = "vk" in (env_list("CONTENT_TARGETS") or ["vk", "tg"])  # стену ВК ведёт только такой экземпляр
        group_id = (await poster.call("groups.getById"))["groups"][0]["id"] if vk_side else 0
        c = Content(db, llm, reader, bot, poster, group_id, http)
        lp = await bot.call("groups.getLongPollSettings", group_id=bot.group_id)
        c.callback = bool(lp.get("events", {}).get("message_event"))
        if not c.callback:
            log.warning("в Long Poll сообщества согласования выключено «Действие с сообщением» — кнопки под "
                        "карточками будут обычными (нажатие остаётся сообщением в чате)")
        src = [f"{r['target']}:{r['name']}" for r in await db.fetch("SELECT target, name FROM content.sources")]
        log.info("веду площадки %s | ВК: club%s | Telegram: %s | источники %s | согласование в club%s",
                 c.targets, group_id or "—", env("TG_CHANNEL_ID") or "—", src, bot.group_id)
        await asyncio.gather(poll_loop(c), listen_loop(c), sweep_loop(c))
    finally:
        for x in (reader, poster, bot, llm):
            await x.close()
        await http.aclose()
        db.terminate()  # не ждём: одно соединение держит блокировку «единственной копии»


if __name__ == "__main__":
    asyncio.run(main())
