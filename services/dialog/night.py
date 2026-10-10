"""Рабочие часы Макса и ночной режим (чистые функции, покрыты тестами)."""
from datetime import datetime, time, timedelta, timezone

from shared.config import env

TZ = timezone(timedelta(hours=int(env("OWNER_UTC_OFFSET", "3"))))   # Москва по умолчанию
DAY_START = int(env("OWNER_DAY_START", "8"))
DAY_END = int(env("OWNER_DAY_END", "20"))
REPING_AFTER = timedelta(minutes=5)      # ночью: повторный алерт, если никто не ответил
DEFER_AFTER_PING = timedelta(minutes=10)  # ночью: после повторного алерта ещё 10 минут — «ответим утром»
NEED_TIME_AFTER = timedelta(hours=1)     # Макс не отвечает клиенту час — «нужно время» и снова пинг
PING_EVERY = timedelta(hours=1)          # днём пингуем Макса раз в час, пока клиент ждёт
RETURN_AFTER = timedelta(hours=8)        # чат у человека 8 ч без его сообщений — снова у бота
OWNER_IDLE = timedelta(minutes=10)       # Макс писал в чате сам и 10 мин молчит — снова у бота (09.10, было 8 ч)

DEFER_REPLY = (f"Сейчас не получается быстро решить этот вопрос — ответим Вам утром, после {DAY_START}:00. "
               "Спасибо за терпение 🙏")
NEED_TIME_REPLY = "Нужно ещё немного времени — скоро вернёмся с ответом. Спасибо за терпение 🙏"
NIGHT_PAY_REPLY = f"Спасибо! Поступление проверим утром, после {DAY_START}:00, и сразу подключим 🙏"
MORNING_ACCESS = f"Доступ пришлём утром, после {DAY_START}:00."   # оплата на ИП пришла ночью, учётку выдаёт человек


def is_night(at: datetime) -> bool:
    h = at.astimezone(TZ).hour
    return not DAY_START <= h < DAY_END


def last_night(now: datetime) -> tuple[datetime, datetime]:
    """Границы последней ночи (вчера DAY_END → сегодня DAY_START) в UTC."""
    local = now.astimezone(TZ)
    end = datetime.combine(local.date(), time(DAY_START), TZ)
    if local < end:
        end -= timedelta(days=1)
    return end - timedelta(hours=24 - DAY_END + DAY_START), end


def wait_action(kind: str, since: datetime, now: datetime, pinged: datetime | None = None,
                nudged: datetime | None = None, deferred: datetime | None = None) -> str | None:
    """Клиент ждёт человека с момента since. Что сделать: 'ping' — алерт Максу; 'nudge' — клиенту «нужно время»
    + алерт; 'defer' — клиенту «ответим утром» (+ алерт); None — ждём.
    kind: 'owner' — Макс сам ведёт чат в Avito; 'handoff' — бот передал вопрос (первый алерт уже ушёл).
    pinged/nudged/deferred — что уже сделано; раньше since — относится к прошлому ожиданию, не в счёт."""
    pinged, nudged, deferred = (x if x and x >= since else None for x in (pinged, nudged, deferred))
    if is_night(now):
        if deferred:
            return None
        if kind == "owner":                      # Макс сам в чате: час тишины — «ответим утром»
            return "defer" if now - since >= NEED_TIME_AFTER else None
        night_ping = pinged if pinged and is_night(pinged) else None   # дневные случаи в 20:00 — сначала повтор
        if not night_ping:
            return "ping" if now - since >= REPING_AFTER else None
        return "defer" if now - night_ping >= DEFER_AFTER_PING else None
    if now - since < NEED_TIME_AFTER:
        return None
    if not nudged and not deferred:
        return "nudge"
    last = max(x for x in (pinged, nudged, deferred) if x)
    return "ping" if now - last >= PING_EVERY else None
