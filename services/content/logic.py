"""Чистая логика автопостинга: какие посты брать, как выглядит карточка согласования и меню."""
import re
from datetime import datetime, timedelta

import httpx

from shared.vk import TEMPORARY, VKError, button, keyboard

POLL_SECONDS = 15 * 60   # как часто проверяем источники
EXPIRE_HOURS = 8         # несогласованный пост сгорает
TEMP_MINUTES = 10        # служебные ответы бота удаляются из чата (карточки ждущих постов остаются)
QUEUE_HOURS = 24         # одобренный пост, который VK временно не даёт опубликовать, ждёт столько, потом снимается
STATUS = {"pending": "ждёт согласования", "publishing": "публикуется", "published": "опубликован",
          "skipped": "пропущен", "expired": "сгорел (8 часов без ответа)",
          "queued": "в очереди: VK временно не даёт публиковать"}


TOKEN_HOUR = 10          # каждое утро (МСК) бот просит обновить суточный токен VK для фото
TOKEN_REPEAT_HOURS = 2   # не обновили — напоминает снова, пока не кончится рабочий день


class TokenNeeded(Exception):
    """Нет действующего токена пользователя VK — одобренные посты ждут в очереди, пока его не пришлют в чат."""


def retryable(e: Exception) -> bool:
    """Временный сбой (сеть, flood control, нет токена) — одобренный пост ждёт в очереди и публикуется повтором сам."""
    return (isinstance(e, (httpx.TransportError, TokenNeeded))
            or (isinstance(e, VKError) and e.code in TEMPORARY))


def token_reminder_due(now: datetime, exp: float, last: float, day_end: int) -> bool:
    """Пора ли просить токен: с TOKEN_HOUR до конца рабочего дня, если токен не доживёт до завтрашнего утреннего
    напоминания (сегодня ещё не обновляли) и с прошлого напоминания прошло TOKEN_REPEAT_HOURS. now — время владельца."""
    tomorrow = now.replace(hour=TOKEN_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=1, hours=-1)
    return (TOKEN_HOUR <= now.hour < day_end and exp < tomorrow.timestamp()
            and now.timestamp() - last >= TOKEN_REPEAT_HOURS * 3600)


def token_request(link: str, exp: float, now: datetime, user_id: str) -> str:
    state = (f"действует до {datetime.fromtimestamp(exp, now.tzinfo):%d.%m %H:%M}" if exp > now.timestamp()
             else "истёк — посты ВК ждут в очереди")
    who = f" (vk.com/id{user_id})" if user_id else ""
    return ("🔑 Обновите токен VK для фото в постах — раз в сутки.\n"
            f"Сейчас токен {state}.\n\n"
            f"1. Откройте ссылку там, где в VK выполнен вход под аккаунтом сообщества{who}:\n{link}\n"
            "2. Нажмите «Разрешить».\n"
            "3. Скопируйте адрес открывшейся страницы и пришлите его сюда ответным сообщением.")


def queued_text(post_id: int, reason: str) -> str:
    return (f"⏳ Пост №{post_id} в очереди: {reason}\nОпубликую сам, как только VK разрешит (проверяю каждые "
            f"{POLL_SECONDS // 60} мин, жду до {QUEUE_HOURS} ч).")


def candidates(items: list[dict], since_ts: float, known: set[str]) -> list[dict]:
    """Новые посты источника от старых к новым: не закреплённые, новее запуска, ещё не виденные."""
    fresh = [p for p in items if not p.get("is_pinned") and p["date"] > since_ts
             and f"{p['owner_id']}_{p['id']}" not in known]
    return sorted(fresh, key=lambda p: p["date"])


def skip_reason(post: dict) -> str | None:
    """Почему пост не берём вовсе (рекламу не отсекаем — её только помечаем при согласовании)."""
    if post.get("copy_history"):
        return "репост чужой записи"
    if any(x["type"] == "video" for x in post.get("attachments", [])):
        return "пост с видео"
    if not (post.get("text") or "").strip():
        return "без текста"
    return None


def source_link(source_key: str) -> str:
    if source_key.startswith("tg:"):  # tg:<канал>_<номер>
        name, num = source_key[3:].rsplit("_", 1)
        return f"https://t.me/{name}/{num}"
    return f"https://vk.com/wall{source_key}"


def card(row: dict, note: str = "") -> str:
    where = "Telegram" if row.get("target") == "tg" else "ВК"
    lines = [f"📝 Пост №{row['id']} на согласование → {where}", f"Источник: {source_link(row['source_key'])}"]
    if row["ad"]:
        lines.append(f"⚠ Возможна реклама: {row['ad']}")
    if note:
        lines.append(f"❗ {note}")
    lines.append(f"Сгорит через {EXPIRE_HOURS} ч без ответа.")
    return "\n".join(lines) + "\n\n" + row["text"]


def card_keyboard(post_id: int, callback: bool = False, target: str = "vk") -> dict:
    """Площадка в команде нужна, когда ВК и Telegram ведут разные экземпляры: у каждого своя нумерация постов."""
    return keyboard([[button("✅ Опубликовать", f"pub:{target}:{post_id}", "positive", callback),
                      button("❌ Пропустить", f"skip:{target}:{post_id}", "negative", callback)]], inline=True)


def parse_decision(cmd: str) -> tuple[str, str | None, int] | None:
    """«pub:tg:5» → ("pub", "tg", 5); старый вид «pub:5» → ("pub", None, 5); не решение по посту → None."""
    parts = cmd.split(":")
    if parts[0] not in ("pub", "skip") or len(parts) not in (2, 3) or not parts[-1].isdigit():
        return None
    return parts[0], (parts[1] if len(parts) == 3 else None), int(parts[-1])


def menu(approve_vk: bool, approve_tg: bool) -> dict:
    def toggle(name: str, on: bool, cmd: str) -> dict:
        return button(f"{name}: согласование {'ВКЛ' if on else 'ВЫКЛ'}", cmd, "positive" if on else "negative")
    return keyboard([[toggle("ВК", approve_vk, "toggle_vk")], [toggle("ТГ", approve_tg, "toggle_tg")],
                     [button("📋 Ждут согласования", "pending"), button("📚 Источники", "sources")]])


LINK_FORMAT = ("Формат ссылки:\n"
               "https://vk.com/название — сообщество ВК, посты пойдут на стену ВК\n"
               "https://t.me/название — Telegram-канал, посты пойдут в Telegram-канал")
_VK = re.compile(r"(?:https?://)?(?:m\.)?vk\.(?:com|ru)/([A-Za-z0-9_.]{2,})/?(?:[?#].*)?$")
_TG = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?([A-Za-z0-9_]{4,})/?(?:[?#].*)?$")


def parse_source(text: str) -> tuple[str, str] | None:
    """Ссылка на источник → (куда публикуем: vk | tg, короткое имя). Принимаем только ссылки."""
    text = (text or "").strip()
    for target, rx in (("vk", _VK), ("tg", _TG)):
        m = rx.match(text)
        if m:
            return target, m.group(1) if target == "vk" else m.group(1).lower()
    return None


def source_url(target: str, name: str) -> str:
    return f"https://t.me/{name}" if target == "tg" else f"https://vk.com/{name}"


def wall_args(name: str) -> dict:
    """Параметры wall.get для сообщества VK: короткое имя или club123 / public123."""
    m = re.fullmatch(r"(?:club|public)(\d+)", name)
    return {"owner_id": -int(m.group(1))} if m else {"domain": name}


PLATFORM = {"vk": "ВК", "tg": "Telegram"}


def sources_text(rows: list[dict], targets: tuple[str, ...] = ("vk", "tg")) -> str:
    def block(target: str, head: str) -> str:
        items = [f"• {source_url(target, r['name'])}" + (f" — {r['title']}" if r["title"] else "")
                 for r in rows if r["target"] == target]
        return head + "\n" + ("\n".join(items) if items else "— пока нет")
    heads = {"vk": "В ВК (на стену сообщества):", "tg": "В Telegram (в канал):"}
    return ("📚 Откуда копируем\n\n" + "\n\n".join(block(t, heads[t]) for t in ("vk", "tg") if t in targets)
            + "\n\nНовые посты проверяются каждые 15 минут.")


def sources_keyboard() -> dict:
    return keyboard([[button("➕ Добавить", "src_add", "positive", True),
                      button("🗑 Убрать", "src_del", "negative", True)]], inline=True)
