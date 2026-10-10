"""Рассылки в VK-боте: кому (текстом) → текст → «такую уже делали?» (тема, кнопками) → итог → запуск.

Список рассылок, пауза, продолжение, стоп.
"""
from datetime import datetime, timedelta, timezone

from services.broadcast import audience as aud
from shared.llm import LLM
from shared.vk import button, keyboard

CANCEL = keyboard([[button("❌ Отмена", "cancel", "negative")]])
TEXT_TIPS = ("✍️ Теперь пришлите текст рассылки одним сообщением — ровно так, как его увидит клиент.\n\n"
             "Советы: коротко, на «Вы», в конце — вопрос («Подключить?»). Ссылки — просто текстом. "
             "Не больше 1–2 эмодзи.")
STATUS = {"active": "▶️ идёт", "paused": "⏸ на паузе", "done": "✅ завершена", "cancelled": "❌ отменена"}
TOPIC_PROMPT = ("Придумай короткое название темы рассылки (2–4 слова, без кавычек и точки) по её тексту. "
                "Например: «Приглашение в VK», «Продление подписки», «Скидка на Steam». Ответь только названием.")


def renew_examples() -> list[str]:
    """Оба варианта текста продления с примерными датами — для показа и теста."""
    now = datetime.now(timezone.utc)
    return [aud.renew_text(now - timedelta(days=2), 12, now), aud.renew_text(now + timedelta(days=5), 12, now)]


class BroadcastFlow:
    def __init__(self, db, llm: LLM, menu: dict):
        self.db, self.llm, self.menu = db, llm, menu

    async def handle(self, s, cmd: str | None, text: str, say, who: str) -> bool:
        """True — сообщение обработано здесь."""
        if cmd == "bc_menu":
            await say("📣 Рассылки. Что делаем?", keyboard([
                [button("➕ Новая рассылка", "bc_new", "positive")],
                [button("📋 Мои рассылки", "bc_list", "primary")]], inline=True))
            return True
        if cmd in ("bc_new", "bc_refilter"):
            s.mode, s.bc_filter, s.bc_audience, s.bc_message, s.bc_topic = "bc_filter", None, None, None, None
            s.bc_kind = None
            await say(aud.HINT if cmd == "bc_new" else "Хорошо, опишите ещё раз, КОМУ отправить.", CANCEL)
            return True
        if (cmd or "").startswith("bc_auto:"):   # кнопки утренней сводки
            return await self.auto(s, cmd.split(":", 1)[1], say)
        if cmd == "bc_ok" and s.bc_filter is not None:
            s.mode = "bc_text"
            await say(TEXT_TIPS, CANCEL)
            return True
        if (cmd or "").startswith("bc_topic") and s.bc_message:
            if cmd == "bc_topic_new":
                s.bc_topic = (await self.llm.chat([{"role": "system", "content": TOPIC_PROMPT},
                                                   {"role": "user", "content": s.bc_message}],
                                                  temperature=0.2, max_tokens=30)).strip(" «»\"'.\n")[:60]
            else:
                s.bc_topic = s.bc_topics[int(cmd.split(":")[1])]
            return await self.summary(s, say)
        if cmd == "bc_test" and s.bc_message:
            for text in (renew_examples() if s.bc_kind == "renew" else [s.bc_message]):
                n = await self.db.execute(
                    "INSERT INTO gateway.outbox (account_id, chat_id, text) "
                    "SELECT user_id, unnest(whitelist), $1 FROM gateway.accounts LIMIT 1", text)
            await say(f"🧪 Отправил в тестовый чат ({n.split()[-1]} шт.) — посмотрите в Avito, как выглядит. "
                      "Запустить можно кнопкой выше.")
            return True
        if cmd == "bc_launch" and s.bc_message and s.bc_topic:
            return await self.launch(s, say, who)
        if cmd == "bc_list":
            return await self.show_list(say)
        if cmd and cmd.split(":")[0] in ("bc_pause", "bc_resume", "bc_stop"):
            action, cid = cmd.split(":")
            status = {"bc_pause": "paused", "bc_resume": "active", "bc_stop": "cancelled"}[action]
            await self.db.execute("UPDATE broadcast.campaigns SET status=$2, finished_at=CASE WHEN $2='cancelled' "
                                  "THEN now() END WHERE id=$1 AND status IN ('active','paused')", int(cid), status)
            if status == "cancelled":
                await self.db.execute("UPDATE broadcast.recipients SET status='cancelled' "
                                      "WHERE campaign_id=$1 AND status='queued'", int(cid))
            await say(f"Рассылка №{cid}: {STATUS[status]}.", self.menu)
            return True

        if s.mode == "bc_filter" and text and not cmd:
            return await self.on_filter(s, text, say)
        if s.mode == "bc_text" and text and not cmd:
            s.bc_message, s.mode = text, "bc_topic"
            return await self.ask_topic(s, say)
        return False

    async def on_filter(self, s, text: str, say) -> bool:
        await say("⏳ Разбираю описание и считаю клиентов…")
        f = await aud.parse(self.llm, text)
        warn = f"\n\n⚠ Не понял и не учёл: «{f.not_understood}»" if f.not_understood else ""
        if f.is_empty():  # не понял, кому — никогда не предлагаем «всю базу» по ошибке
            await say("🤔 Не понял, кому отправить. Напишите, например:\n"
                      "— Кто писал за последние 5 дней, но не купил\n— Клиенту «Иван» (имя в Avito)\n"
                      "— Все, кто покупал Game Pass" + warn, CANCEL)
            return True
        a = await aud.select(self.db, f)
        if not a.rows and f.item_contains and not f.client_name:  # «недвижимость МО» — может, это имя клиента?
            by_name = aud.Filter(**{**f.__dict__, "item_contains": None, "client_name": f.item_contains})
            if (b := await aud.select(self.db, by_name)).rows:
                f, a = by_name, b
        s.bc_filter, s.bc_audience, s.bc_filter_text = f, a, text
        if not a.rows:
            await say(f"Понял так:\n{aud.describe(f)}{warn}\n\n🤷 Под это никто не подходит "
                      f"(кто пишет прямо сейчас — не считаю). Опишите по-другому.", CANCEL)
            return True
        await say(f"Понял так:\n{aud.describe(f)}{warn}\n\n👥 Подходит клиентов: {len(a.rows)}\n\nВсё верно?",
                  keyboard([[button("✅ Верно, дальше", "bc_ok", "positive")],
                            [button("✏️ Описать заново", "bc_refilter"),
                             button("❌ Отмена", "cancel", "negative")]], inline=True))
        return True

    async def ask_topic(self, s, say) -> bool:
        s.bc_topics = [r["topic"] for r in await self.db.fetch(
            "SELECT topic FROM broadcast.campaigns GROUP BY topic ORDER BY max(id) DESC LIMIT 4")]
        kb = [[button(f"🔁 {t}"[:40], f"bc_topic:{i}")] for i, t in enumerate(s.bc_topics)]
        kb.append([button("🆕 Такой ещё не было", "bc_topic_new", "positive")])
        await say("Такую рассылку уже делали?\n\nЕсли да — нажмите её: кто уже получил, повторно не получит, пока "
                  "рассылка не обойдёт всю подходящую базу.\nЕсли нет — «🆕 Такой ещё не было».",
                  keyboard(kb, inline=True))
        return True

    async def auto(self, s, kind: str, say) -> bool:
        """Готовая рассылка из утренней сводки: аудитория и текст уже заданы → проверка → запуск."""
        s.bc_kind, s.bc_topics = kind, []
        if kind == "renew":
            s.bc_filter, s.bc_filter_text, s.bc_topic = aud.Filter(), aud.RENEW_WHO, aud.RENEW_TOPIC
            s.bc_message = "\n\n— или —\n\n".join(renew_examples())
            return await self.renew_summary(s, say)
        if kind not in aud.PRESETS:
            return False
        s.bc_topic, s.bc_filter_text, f, s.bc_message = aud.PRESETS[kind]
        free = await aud.spare(self.db, len(await aud.renewals(self.db)))
        if not free:
            await say(f"На ближайший день очередь уже полная ({aud.DAILY_LIMIT}) — эту рассылку лучше завтра.",
                      self.menu)
            return True
        s.bc_filter = aud.Filter(**{**f.__dict__, "max_recipients": free})   # добиваем дневной лимит
        return await self.summary(s, say)

    async def renew_summary(self, s, say) -> bool:
        rows = await aud.renewals(self.db)
        if not rows:
            await say("Продлевать сейчас некого — обо всех подписках уже напомнили.", self.menu)
            return True
        ended = sum(r["expires_at"] <= datetime.now(timezone.utc) for r in rows)
        await say(f"Проверьте рассылку:\n\n👥 Кому: подписка закончилась за 30 дней — {ended}, "
                  f"заканчивается в ближайшие 7 дней — {len(rows) - ended}\n"
                  f"🏷 Тема: «{s.bc_topic}» — по каждой подписке одно напоминание\n📨 Получат: {len(rows)}\n"
                  f"📅 Займёт ~{aud.eta_days(len(rows))} дн. (до {aud.DAILY_LIMIT} в день, с 8 до 20, "
                  f"одному клиенту не чаще раза в {aud.CLIENT_COOLDOWN} дн.)\n\n"
                  f"💬 Текст считается в момент отправки (если к тому времени продлил — не пишем):\n{s.bc_message}",
                  keyboard([[button("🚀 Запустить", "bc_launch", "positive")],
                            [button("🧪 Тест в тестовый чат", "bc_test")],
                            [button("❌ Отмена", "cancel", "negative")]], inline=True))
        return True

    async def summary(self, s, say) -> bool:
        a = await aud.select(self.db, s.bc_filter, s.bc_topic)
        s.bc_audience = a
        if not a.rows:
            await say(f"🏷 Тема «{s.bc_topic}»: все подходящие клиенты её уже получили в этом круге. "
                      "Опишите другую аудиторию или начните новую рассылку.", self.menu)
            s.mode = "menu"
            return True
        again = (f"\n🔁 Уже получили эту тему: {a.done_in_round} — им не шлём (круг №{a.round_no})"
                 if a.done_in_round else "")
        await say(f"Проверьте рассылку:\n\n👥 Кому: \n{aud.describe(s.bc_filter)}\n\n"
                  f"🏷 Тема: «{s.bc_topic}»{again}\n📨 Получат: {len(a.rows)}\n"
                  f"📅 Займёт ~{aud.eta_days(len(a.rows))} дн. (до {aud.DAILY_LIMIT} в день, с 8 до 20)\n\n"
                  f"💬 Текст:\n{s.bc_message}",
                  keyboard([[button("🚀 Запустить", "bc_launch", "positive")],
                            [button("🧪 Тест в тестовый чат", "bc_test")],
                            [button("✏️ Изменить текст", "bc_ok"), button("❌ Отмена", "cancel", "negative")]],
                           inline=True))
        return True

    async def launch(self, s, say, who: str) -> bool:
        renew = s.bc_kind == "renew"
        if renew:   # получатель — подписка: чат покупки, текст соберёт воркер
            rows = [(r["client_id"], r["chat_id"], r["id"]) for r in await aud.renewals(self.db)]
        else:
            rows = [(c, ch, None) for c, ch in (await aud.select(self.db, s.bc_filter, s.bc_topic)).rows]
        if not rows:   # пересчитали на момент запуска
            await say("Получателей не осталось — ничего не запускаю.", self.menu)
            return True
        async with self.db.acquire() as con, con.transaction():
            cid = await con.fetchval(
                "INSERT INTO broadcast.campaigns (filter_text, filter, message, created_by, topic, kind) "
                "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id", s.bc_filter_text, s.bc_filter.to_json(),
                f"{aud.RENEW_ENDED}\n— или —\n{aud.RENEW_SOON}" if renew else s.bc_message, who, s.bc_topic,
                "renew" if renew else "custom")
            await con.executemany(
                "INSERT INTO broadcast.recipients (campaign_id, client_id, chat_id, priority, purchase_id) "
                "VALUES ($1,$2,$3,$4,$5)", [(cid, c, ch, i, p) for i, (c, ch, p) in enumerate(rows)])
        s.mode, s.bc_filter, s.bc_audience, s.bc_message, s.bc_topic, s.bc_kind = "menu", None, None, None, None, None
        await say(f"🚀 Рассылка №{cid} запущена: {len(rows)} получателей, ~{aud.eta_days(len(rows))} дн.\n"
                  f"Отправляю равномерно с 8 до 20, не больше {aud.DAILY_LIMIT} в день. "
                  "Прогресс и пауза — «📣 Рассылки» → «📋 Мои рассылки».", self.menu)
        return True

    async def show_list(self, say) -> bool:
        rows = await self.db.fetch("""
            SELECT c.id, c.status, c.filter_text, c.topic,
                   count(*) FILTER (WHERE r.status='sent') AS sent, count(r.*) AS total
            FROM broadcast.campaigns c LEFT JOIN broadcast.recipients r ON r.campaign_id = c.id
            GROUP BY c.id ORDER BY c.id DESC LIMIT 5""")
        if not rows:
            await say("Рассылок пока не было. Создать — «➕ Новая рассылка».", self.menu)
            return True
        lines, kb = [], []
        for r in rows:
            lines.append(f"№{r['id']} «{r['topic']}» {STATUS.get(r['status'], r['status'])} — "
                         f"{r['sent']}/{r['total']}\n   кому: {r['filter_text'][:80]}")
            if r["status"] == "active":
                kb.append([button(f"⏸ Пауза №{r['id']}", f"bc_pause:{r['id']}"),
                           button(f"❌ Стоп №{r['id']}", f"bc_stop:{r['id']}", "negative")])
            elif r["status"] == "paused":
                kb.append([button(f"▶️ Продолжить №{r['id']}", f"bc_resume:{r['id']}", "positive"),
                           button(f"❌ Стоп №{r['id']}", f"bc_stop:{r['id']}", "negative")])
        await say("📋 Последние рассылки (отправлено/всего):\n\n" + "\n".join(lines),
                  keyboard(kb, inline=True) if kb else self.menu)
        return True
