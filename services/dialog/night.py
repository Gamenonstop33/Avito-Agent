"""Рабочие часы Макса и ночной режим (чистые функции, покрыты тестами)."""
from datetime import datetime, time, timedelta, timezone

from shared.config import env

TZ = timezone(timedelta(hours=int(env("OWNER_UTC_OFFSET", "3"))))   # Москва по умолчанию
DAY_START = int(env("OWNER_DAY_START", "8"))     # рабочий день — рассылки и сводки (8–20)
DAY_END = int(env("OWNER_DAY_END", "20"))
NIGHT_FROM = int(env("OWNER_NIGHT_FROM", "23"))   # с этого часа до NIGHT_TO бот один, Макс не отвечает (14.10)
NIGHT_TO = int(env("OWNER_NIGHT_TO", "6"))        # конец ночи бота (29.10, было 8: оплата в 07:01 осталась без алерта)
REMIND_AFTER = timedelta(minutes=15)     # днём Макс не ответил — одно напоминание (по чату не больше двух алертов)
NEED_TIME_AFTER = timedelta(hours=1)     # клиент ждёт час — ему «нужно ещё немного времени» (без алерта)
RETURN_AFTER = timedelta(hours=8)        # чат у человека 8 ч без его сообщений — снова у бота
OWNER_IDLE = timedelta(minutes=10)       # Макс писал в чате сам и 10 мин молчит — снова у бота (09.10, было 8 ч)
OWNER_AWAKE = timedelta(minutes=30)      # ночью Макс писал в Avito / жал кнопки в VK за это время — уже день (18.10)
PAY_NIGHT_WAIT = timedelta(minutes=10)   # ночью «оплатил» на карту: 10 мин без владельца — клиенту «утром» (21.10)

DEFER_REPLY = (f"Сейчас не получается быстро решить этот вопрос — ответим Вам утром, после {DAY_START}:00. "
               "Спасибо за терпение 🙏")
NEED_TIME_REPLY = "Нужно ещё немного времени — скоро вернёмся с ответом. Спасибо за терпение 🙏"
# ночью «оплатил», а владелец 10 мин не отвечает (решение 21.10: не «утром», а «проблемы, вернёмся чуть позже»)
NIGHT_PAY_REPLY = "Возникли небольшие технические проблемы с проверкой оплаты — вернёмся к Вам чуть позже 🙏"
MORNING_ACCESS = "Доступ подготовим как можно оперативнее и напишем здесь."   # ИП ночью, учётку выдаёт человек
NIGHT_PRESALE = "Подключим как можно оперативнее 🙏"   # ночью до оплаты на «подключите сейчас?» (решение 21.10)


def is_night(at: datetime) -> bool:
    """Ночь бота: NIGHT_FROM–NIGHT_TO (23–6) — Макса не зовём, всё — в утреннюю сводку (в DAY_START)."""
    h = at.astimezone(TZ).hour
    return not NIGHT_TO <= h < NIGHT_FROM


def in_workday(at: datetime) -> bool:
    """Рабочий день для рассылок и сводок: DAY_START–DAY_END (8–20)."""
    return DAY_START <= at.astimezone(TZ).hour < DAY_END


def last_night(now: datetime) -> tuple[datetime, datetime]:
    """Границы последней ночи для сводки (вчера NIGHT_FROM → сегодня DAY_START) в UTC."""
    local = now.astimezone(TZ)
    end = datetime.combine(local.date(), time(DAY_START), TZ)
    if local < end:
        end -= timedelta(days=1)
    return end - timedelta(hours=24 - NIGHT_FROM + DAY_START), end


def wait_action(kind: str, since: datetime, now: datetime, pinged: datetime | None = None,
                nudged: datetime | None = None, deferred: datetime | None = None,
                dark: bool | None = None, reason: str | None = None) -> str | None:
    """Клиент ждёт человека с момента since (первый алерт уже ушёл). Что сделать: 'ping' — одно напоминание Максу
    через 15 мин (днём); 'nudge' — клиенту «нужно ещё немного времени» через час, без алерта; 'defer' — ночью
    клиенту «ответим утром», без алерта (утром — сводка); None — ждём.
    kind: 'owner' — Макс сам ведёт чат в Avito; 'handoff' — бот передал вопрос.
    pinged/nudged/deferred — что уже сделано; раньше since — относится к прошлому ожиданию, не в счёт.
    dark — ночной режим (ночь и Макс не на связи, Dialog.dark); не задан — просто по часам.
    reason — причина передачи: оплату ночью ждём PAY_NIGHT_WAIT, потом «проверим утром» (решение 21.10)."""
    pinged, nudged, deferred = (x if x and x >= since else None for x in (pinged, nudged, deferred))
    if is_night(now) if dark is None else dark:
        if deferred:
            return None
        if kind == "owner":                      # Макс сам в чате: час тишины — «ответим утром»
            return "defer" if now - since >= NEED_TIME_AFTER else None
        if reason == "payment":                  # оплата: 10 мин — вдруг владелец на связи, потом «проверим утром»
            return "defer" if now - since >= PAY_NIGHT_WAIT else None
        return "defer"                           # ночью Макс не отвечает — клиенту сразу «ответим утром»
    if not pinged and now - since >= REMIND_AFTER:
        return "ping"
    if not nudged and not deferred and now - since >= NEED_TIME_AFTER:
        return "nudge"
    return None
