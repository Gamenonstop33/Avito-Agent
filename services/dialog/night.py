"""Рабочие часы Макса и ночной режим (чистые функции, покрыты тестами)."""
from datetime import datetime, time, timedelta, timezone

from shared.config import env

TZ = timezone(timedelta(hours=int(env("OWNER_UTC_OFFSET", "3"))))   # Москва по умолчанию
DAY_START = int(env("OWNER_DAY_START", "8"))
DAY_END = int(env("OWNER_DAY_END", "20"))
NIGHT_FROM = int(env("OWNER_NIGHT_FROM", "23"))   # с этого часа до DAY_START бот один, Макс не отвечает (14.10)
REMIND_AFTER = timedelta(minutes=15)     # днём Макс не ответил — одно напоминание (по чату не больше двух алертов)
NEED_TIME_AFTER = timedelta(hours=1)     # клиент ждёт час — ему «нужно ещё немного времени» (без алерта)
RETURN_AFTER = timedelta(hours=8)        # чат у человека 8 ч без его сообщений — снова у бота
OWNER_IDLE = timedelta(minutes=10)       # Макс писал в чате сам и 10 мин молчит — снова у бота (09.10, было 8 ч)

DEFER_REPLY = (f"Сейчас не получается быстро решить этот вопрос — ответим Вам утром, после {DAY_START}:00. "
               "Спасибо за терпение 🙏")
NEED_TIME_REPLY = "Нужно ещё немного времени — скоро вернёмся с ответом. Спасибо за терпение 🙏"
NIGHT_PAY_REPLY = f"Спасибо! Поступление проверим утром, после {DAY_START}:00, и сразу подключим 🙏"
MORNING_ACCESS = f"Доступ пришлём утром, после {DAY_START}:00."   # оплата на ИП пришла ночью, учётку выдаёт человек


def is_night(at: datetime) -> bool:
    """Ночь бота: NIGHT_FROM–DAY_START (23–8) — Макса не зовём, всё — в утреннюю сводку."""
    h = at.astimezone(TZ).hour
    return not DAY_START <= h < NIGHT_FROM


def in_workday(at: datetime) -> bool:
    """Рабочий день для рассылок и сводок: DAY_START–DAY_END (8–20)."""
    return DAY_START <= at.astimezone(TZ).hour < DAY_END


def last_night(now: datetime) -> tuple[datetime, datetime]:
    """Границы последней ночи (вчера NIGHT_FROM → сегодня DAY_START) в UTC."""
    local = now.astimezone(TZ)
    end = datetime.combine(local.date(), time(DAY_START), TZ)
    if local < end:
        end -= timedelta(days=1)
    return end - timedelta(hours=24 - NIGHT_FROM + DAY_START), end


def wait_action(kind: str, since: datetime, now: datetime, pinged: datetime | None = None,
                nudged: datetime | None = None, deferred: datetime | None = None) -> str | None:
    """Клиент ждёт человека с момента since (первый алерт уже ушёл). Что сделать: 'ping' — одно напоминание Максу
    через 15 мин (днём); 'nudge' — клиенту «нужно ещё немного времени» через час, без алерта; 'defer' — ночью
    клиенту «ответим утром», без алерта (утром — сводка); None — ждём.
    kind: 'owner' — Макс сам ведёт чат в Avito; 'handoff' — бот передал вопрос.
    pinged/nudged/deferred — что уже сделано; раньше since — относится к прошлому ожиданию, не в счёт."""
    pinged, nudged, deferred = (x if x and x >= since else None for x in (pinged, nudged, deferred))
    if is_night(now):
        if deferred:
            return None
        if kind == "owner":                      # Макс сам в чате: час тишины — «ответим утром»
            return "defer" if now - since >= NEED_TIME_AFTER else None
        return "defer"                           # ночью Макс не отвечает — клиенту сразу «ответим утром»
    if not pinged and now - since >= REMIND_AFTER:
        return "ping"
    if not nudged and not deferred and now - since >= NEED_TIME_AFTER:
        return "nudge"
    return None
