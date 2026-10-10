"""Чистая логика без сети и БД (покрыта тестами)."""
import random
from datetime import datetime, timedelta, timezone


def ts(unix: int | float) -> datetime:
    return datetime.fromtimestamp(unix, tz=timezone.utc)


def origin_of(m: dict, bot_ids: set[str]) -> str:
    if m.get("type") == "system":
        return "system"
    if m.get("direction") == "in":
        return "client"
    return "bot" if m["id"] in bot_ids else "owner"


DELETED_TEXT = "Сообщение удалено"   # так Avito отдаёт удалённое сообщение (тот же id, тип text)


def event_kind(origin: str, created: datetime, started_at: datetime | None, text: str | None = None,
               stopped_at: datetime | None = None) -> str | None:
    """Какое событие отдать в dialog. Старые сообщения (до запуска бота) не трогаем.
    Бот остановлен — отдаём только сообщения Макса после остановки: чат за ним и после «▶️ Старт»."""
    if text == DELETED_TEXT:
        return None
    if started_at is None:
        return "owner_message" if origin == "owner" and stopped_at and created >= stopped_at else None
    if created < started_at:
        return None
    if origin == "system" and text and "не отправилось" in text.lower():
        return "delivery_failed"  # Avito не доставил наше сообщение (мат, ссылка, контакты и т.п.)
    return {"client": "client_message", "owner": "owner_message"}.get(origin)


def chat_allowed(chat_id: str, all_chats: bool, whitelist: list[str]) -> bool:
    return all_chats or chat_id in whitelist


def reply_delay(now: datetime, lo: int = 5, hi: int = 20) -> datetime:
    """Когда отправлять ответ: «как живой человек», 5–20 с."""
    return now + timedelta(seconds=random.uniform(lo, hi))



MAX_TEXT = 1000   # длиннее Avito отвечает 400 (09.10: так не дошли 6 приветствий с прайсом за 3 дня)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Режем по последнему разрыву абзаца, иначе строки, иначе пробелу до лимита."""
    parts = []
    while len(text) > limit:
        cut = next((i for sep in ("\n\n", "\n", " ") if (i := text.rfind(sep, 0, limit + 1)) > 0), limit)
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + [text] if text else parts
