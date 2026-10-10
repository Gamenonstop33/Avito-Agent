"""broadcast: отправляет рассылки по очереди — не больше DAILY_LIMIT в рабочий день, равномерно.

Запуск: python -m services.broadcast.worker
"""
import asyncio
import json
import logging
import random
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from services.broadcast import audience as aud
from services.broadcast import purchases
from services.avito_gateway.logic import origin_of, ts
from services.broadcast.audience import ACTIVE_SKIP, CLIENT_COOLDOWN, DAILY_LIMIT
from services.dialog import night
from shared import alerts
from shared.avito import AvitoClient
from shared.config import env
from shared.db import connect, single_instance
from shared.llm import LLM
from shared.vk import button, keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("broadcast")
SCHEMA = purchases.SCHEMA + (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
SUMMARY_HOUR = 10   # по Москве: сводка в VK — сколько подписок закончилось/заканчивается, кнопки рассылок


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    d = now.astimezone(night.TZ).date()
    return (datetime.combine(d, time(night.DAY_START), night.TZ), datetime.combine(d, time(night.DAY_END), night.TZ))


def next_send_at(now: datetime, last_sent: datetime | None, sent_today: int, jitter: float = 1.0) -> datetime | None:
    """Когда можно отправить следующее: остаток дня делим поровну на оставшуюся квоту. None — сегодня больше нельзя."""
    start, end = day_bounds(now)
    remaining = DAILY_LIMIT - sent_today
    if night.is_night(now) or remaining <= 0:
        return None
    if last_sent is None or last_sent < start:
        return now
    return last_sent + (end - now) / remaining * jitter


NEXT_RECIPIENT = f"""
SELECT r.campaign_id, r.client_id, r.chat_id, r.purchase_id, c.message, c.kind
FROM broadcast.recipients r
JOIN broadcast.campaigns c ON c.id = r.campaign_id AND c.status = 'active'
WHERE r.status = 'queued'            -- белый список рассылки не ограничивает (решение 03.10), только ответы бота
  AND NOT EXISTS (SELECT 1 FROM broadcast.recipients s WHERE s.client_id = r.client_id AND s.status = 'sent'
                  AND s.sent_at > now() - interval '{CLIENT_COOLDOWN} days')
  AND (c.filter->>'client_name' IS NOT NULL      -- конкретному клиенту Макс пишет осознанно
       OR NOT EXISTS (SELECT 1 FROM gateway.chats ch JOIN gateway.messages m ON m.chat_id = ch.id
                      WHERE ch.client_id = r.client_id   -- любая переписка в любом чате клиента
                        AND m.created_at > now() - interval '{int(ACTIVE_SKIP.total_seconds() // 3600)} hours'))
ORDER BY c.id, r.priority
LIMIT 1"""


async def refresh_client(db, av: AvitoClient, client_id: int) -> None:
    """Чаты вне белого списка gateway не опрашивает — перед отправкой подтягиваем их свежие сообщения из Avito в БД
    (без событий: бот на них не отвечает), чтобы правило «переписка за сутки» видело и их."""
    acc = await db.fetchrow("SELECT user_id, all_chats, whitelist FROM gateway.accounts LIMIT 1")
    if acc["all_chats"]:
        return   # «все чаты»: gateway сам видит все свежие сообщения
    for r in await db.fetch("SELECT id FROM gateway.chats WHERE client_id=$1 AND NOT id = ANY($2::text[])",
                            client_id, acc["whitelist"]):   # белый список не трогаем: вставка «съела» бы событие боту
        msgs = await av.messages(acc["user_id"], r["id"], limit=20)
        bot_ids = {x["message_id"] for x in await db.fetch(
            "SELECT message_id FROM gateway.outbox WHERE message_id = ANY($1::text[])", [m["id"] for m in msgs])}
        await db.executemany(
            "INSERT INTO gateway.messages (id, chat_id, direction, origin, type, text, content, created_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING",
            [(m["id"], r["id"], m.get("direction", "out"), origin_of(m, bot_ids), m.get("type", "text"),
              (m.get("content") or {}).get("text"), json.dumps(m.get("content") or {}, ensure_ascii=False),
              ts(m["created"])) for m in msgs])


async def tick(db, av: AvitoClient | None = None) -> None:
    now = datetime.now(timezone.utc)
    start, _ = day_bounds(now)
    sent_today, last_sent = await db.fetchrow(
        "SELECT count(*), max(sent_at) FROM broadcast.recipients WHERE status='sent' AND sent_at >= $1", start)
    when = next_send_at(now, last_sent, sent_today, random.uniform(0.8, 1.2))
    if when is None or now < when:
        return
    r = await db.fetchrow(NEXT_RECIPIENT)
    if r and av:
        await refresh_client(db, av, r["client_id"])
        again = await db.fetchrow(NEXT_RECIPIENT)   # со свежими сообщениями клиент мог выпасть (переписка за сутки)
        if not again or (again["campaign_id"], again["client_id"]) != (r["campaign_id"], r["client_id"]):
            log.info("рассылка %s → %s: в Avito свежая переписка, клиент подождёт", r["campaign_id"], r["chat_id"])
            r = None
    if r:
        text = r["message"] if r["kind"] != "renew" else await aud.renew_message(db, r["purchase_id"], now)
        if text is None:   # продление: с момента запуска продлил или срок ушёл из окна
            await db.execute("UPDATE broadcast.recipients SET status='skipped' WHERE campaign_id=$1 AND client_id=$2",
                             r["campaign_id"], r["client_id"])
            log.info("рассылка %s → %s: продлевать уже не нужно, пропуск", r["campaign_id"], r["chat_id"])
        else:
            async with db.acquire() as con, con.transaction():
                await con.execute("INSERT INTO gateway.outbox (account_id, chat_id, text, broadcast) "
                                  "SELECT user_id, $1, $2, true FROM gateway.accounts LIMIT 1", r["chat_id"], text)
                await con.execute("UPDATE broadcast.recipients SET status='sent', sent_at=now() "
                                  "WHERE campaign_id=$1 AND client_id=$2", r["campaign_id"], r["client_id"])
            log.info("рассылка %s → %s (%d/%d за день)", r["campaign_id"], r["chat_id"], sent_today + 1, DAILY_LIMIT)
    for c in await db.fetch(
            "UPDATE broadcast.campaigns c SET status='done', finished_at=now() WHERE status='active' AND NOT EXISTS "
            "(SELECT 1 FROM broadcast.recipients r WHERE r.campaign_id=c.id AND r.status='queued') RETURNING id"):
        sent, skipped = await db.fetchrow(
            "SELECT count(*) FILTER (WHERE status='sent'), count(*) FILTER (WHERE status='skipped') "
            "FROM broadcast.recipients WHERE campaign_id=$1", c["id"])
        await alerts.send_alert(db, f"{alerts.TAG_INFO} · 📣 Рассылка №{c['id']} завершена: отправлено {sent}." +
                                (f" Пропущено {skipped}: уже продлили." if skipped else ""))


async def daily_summary(db, llm: LLM, now: datetime) -> None:
    """Раз в день с SUMMARY_HOUR: обновить покупки и сроки, прислать в VK сводку с кнопками рассылок."""
    local = now.astimezone(night.TZ)
    day = local.date().isoformat()
    if not SUMMARY_HOUR <= local.hour < night.DAY_END or await db.fetchval(   # ноутбук проснулся вечером — ждём утра
            "SELECT value FROM broadcast.settings WHERE key='summary_day'") == day:
        return
    await db.execute("INSERT INTO broadcast.settings (key, value) VALUES ('summary_day', $1) "
                     "ON CONFLICT (key) DO UPDATE SET value = $1", day)   # сразу: при ошибке не повторяем весь день
    found, terms = await purchases.sync(db), await purchases.fill_terms(db, llm)
    log.info("сводка: новых покупок %d, разобрано сроков %d", found, terms)
    text, buttons = await aud.daily_offer(db, now)
    kb = keyboard([[button(label, cmd, "positive" if cmd == "bc_auto:renew" else "primary")]
                   for label, cmd in buttons], inline=True) if buttons else None
    await alerts.send_alert(db, f"{alerts.TAG_INFO} · {text}", kb)


async def main() -> None:
    db = await connect(alerts.SCHEMA + SCHEMA)
    await single_instance(db, "broadcast")
    llm, av = LLM(), AvitoClient(env("AVITO_CLIENT_ID"), env("AVITO_CLIENT_SECRET"))
    log.info("broadcast запущен: до %d сообщений в день, %d:00–%d:00, сводка в %d:00", DAILY_LIMIT,
             night.DAY_START, night.DAY_END, SUMMARY_HOUR)
    try:
        while True:
            try:
                await tick(db, av)
                await daily_summary(db, llm, datetime.now(timezone.utc))
            except Exception:
                log.exception("ошибка цикла")
            await asyncio.sleep(20)
    finally:
        db.terminate()  # одно соединение держит блокировку «единственной копии»


if __name__ == "__main__":
    asyncio.run(main())
