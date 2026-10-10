"""Покупки по отдельности: в каком чате, когда, что и на какой срок — для напоминаний о продлении.

Покупка = серия шаблонов Макса после оплаты (crm.PURCHASE_MARKERS). В одном чате бывает несколько покупок:
шаблон позже PURCHASE_GAP после прошлого — новая покупка. Тип и срок LLM находит в переписке перед оплатой
(«14 мес за 4750», или по сумме из прайса) — название объявления не используем. Заказы бота пишет sales.confirm.

Учётку, выданную Максом вручную (нет на складе, история до бота), разбираем из его сообщений кодом (fill_creds).
Запуск (ежедневно — шаг nightly, вечером; вручную — разовая загрузка всего): python -m services.broadcast.purchases
"""
import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta

import asyncpg

from shared import catalog, crm
from shared.llm import LLM
from shared.sales import ISSUED_VIEW, add_months

log = logging.getLogger("purchases")

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS crm;
CREATE TABLE IF NOT EXISTS crm.purchases (
    id         bigserial PRIMARY KEY,
    client_id  bigint NOT NULL,                -- avito user id
    chat_id    text NOT NULL,
    paid_at    timestamptz NOT NULL,           -- первый шаблон после оплаты (для заказа бота — подтверждение)
    product    text,                           -- console | pc | other | none (поддержка, не покупка); NULL — не разобрали
    months     int,                            -- NULL — срок не нашли: о продлении не пишем
    expires_at timestamptz,
    source     text NOT NULL,                  -- chat (шаблоны Макса) | order (заказ бота)
    checked_at timestamptz,                    -- когда искали срок
    UNIQUE (chat_id, paid_at)
);
CREATE INDEX IF NOT EXISTS purchases_client_idx ON crm.purchases (client_id, paid_at);
-- какую учётку выдал Макс (из его сообщений в чате, разбор кодом — без LLM); выдачи со склада — в sales.orders
ALTER TABLE crm.purchases ADD COLUMN IF NOT EXISTS login text;
ALTER TABLE crm.purchases ADD COLUMN IF NOT EXISTS password text;
ALTER TABLE crm.purchases ADD COLUMN IF NOT EXISTS recovery_email text;
ALTER TABLE crm.purchases ADD COLUMN IF NOT EXISTS creds_checked_at timestamptz;
""" + ISSUED_VIEW   # реестр выдачи

PURCHASE_GAP = timedelta(days=7)
TERM_HORIZON = timedelta(days=24 * 31)   # старше — подписка точно закончилась давно, срок не ищем
CONTEXT_MSGS = 40

TERM_SYSTEM = """Перед тобой переписка магазина подписок Xbox Game Pass с клиентом (все его чаты), в конце —
шаблон продавца (инструкция подключения или просьба об отзыве). Верни JSON:
{"purchase": true | false, "product": "console" | "pc" | "other", "months": int | null}
purchase — в этом фрагменте была НОВАЯ оплата (реквизиты или ссылка на оплату, «оплатил», «перевёл», чек).
false — это поддержка уже купленного: не работает, слетела, повторная инструкция, замена аккаунта по гарантии.
console — Game Pass Ultimate для консоли Xbox; pc — PC Game Pass для компьютера;
other — не подписка (игра, ключ, Steam, пополнение кошелька).
months — срок подписки в месяцах, за который заплатили (последний согласованный перед оплатой).
Если срок словами не назван — определи по сумме оплаты по прайсу ниже (цены могли немного отличаться).
Если всё равно не уверен — null. Не выдумывай.

Прайс:
{price}"""


def purchase_starts(msgs: list[tuple[datetime, str | None]]) -> list[datetime]:
    """Начала покупок в чате по сообщениям продавца: шаблоны ближе PURCHASE_GAP к прошлому — та же покупка."""
    starts, last = [], None
    for at, text in sorted(msgs, key=lambda m: m[0]):
        if not crm.is_purchase_msg(text):
            continue
        if last is None or at - last > PURCHASE_GAP:
            starts.append(at)
        last = at
    return starts


def parse_term(d: dict) -> tuple[str | None, int | None]:
    """product 'none' — не покупка (поддержка уже купленного): в продлении и «когда покупал» не участвует."""
    if d.get("purchase") is False:
        return "none", None
    product = d.get("product") if d.get("product") in ("console", "pc", "other") else None
    months = d.get("months")
    months = int(months) if isinstance(months, (int, float)) and 1 <= months <= 36 else None
    return product, (months if product in ("console", "pc") else None)


async def sync(db: asyncpg.Pool) -> int:
    """Находит покупки во всех чатах БД (история + то, что принёс gateway). Возвращает, сколько новых."""
    rows = await db.fetch("""
        SELECT m.chat_id, ch.client_id, m.created_at, m.text FROM gateway.messages m
        JOIN gateway.chats ch ON ch.id = m.chat_id
        WHERE m.origin = 'owner' AND ch.client_id IS NOT NULL AND m.text IS NOT NULL""")
    by_chat: dict[tuple[str, int], list] = defaultdict(list)
    for r in rows:
        by_chat[(r["chat_id"], r["client_id"])].append((r["created_at"], r["text"]))
    found = [(client, chat, at) for (chat, client), msgs in by_chat.items() for at in purchase_starts(msgs)]
    before = await db.fetchval("SELECT count(*) FROM crm.purchases")
    await db.executemany("""
        INSERT INTO crm.purchases (client_id, chat_id, paid_at, source)
        SELECT $1::bigint, $2::text, $3::timestamptz, 'chat' WHERE NOT EXISTS (   -- уже есть (в т.ч. заказ бота)
            SELECT 1 FROM crm.purchases p WHERE p.chat_id = $2 AND p.paid_at BETWEEN $3 - $4::interval AND $3 + $4)
        """, [(client, chat, at, PURCHASE_GAP) for client, chat, at in found])
    return await db.fetchval("SELECT count(*) FROM crm.purchases") - before


async def context(db: asyncpg.Pool, chat_id: str, paid_at: datetime) -> str:
    """Переписка клиента во ВСЕХ его чатах от его прошлой покупки до этой (включая шаблон): срок могли обсудить
    в одном объявлении, а оплатить в другом. Название объявления не берём."""
    rows = await db.fetch("""
        WITH c AS (SELECT client_id FROM gateway.chats WHERE id = $1),
             prev AS (SELECT max(p.paid_at) AS at FROM crm.purchases p, c
                      WHERE p.client_id = c.client_id AND p.paid_at < $2::timestamptz)
        SELECT m.origin, m.text FROM gateway.messages m JOIN gateway.chats ch ON ch.id = m.chat_id, c, prev
        WHERE ch.client_id = c.client_id AND m.created_at > COALESCE(prev.at, '-infinity')
          AND m.created_at <= $2::timestamptz + interval '1 minute'
          AND m.origin IN ('client','owner','bot') AND m.text IS NOT NULL
        ORDER BY m.created_at DESC LIMIT $3""", chat_id, paid_at, CONTEXT_MSGS)
    lines = []
    for r in reversed(rows):
        who = "Клиент" if r["origin"] == "client" else "Продавец"
        lines.append(f"{who}: {re.sub(r'\s+', ' ', r['text'])[:300]}")
    return "\n".join(lines)


async def fill_terms(db: asyncpg.Pool, llm: LLM, parallel: int = 5) -> int:
    """Тип и срок для покупок, где их ещё не искали. Ошибка LLM — попробуем в следующий раз."""
    todo = await db.fetch("SELECT id, chat_id, paid_at FROM crm.purchases WHERE checked_at IS NULL "
                          "AND paid_at > now() - $1::interval ORDER BY paid_at DESC", TERM_HORIZON)
    system = TERM_SYSTEM.replace("{price}", await catalog.price_text(db))
    sem, done = asyncio.Semaphore(parallel), 0

    async def one(p) -> None:
        nonlocal done
        async with sem:
            try:
                text = await context(db, p["chat_id"], p["paid_at"])
                product, months = parse_term(await llm.chat_json(system, text, temperature=0, max_tokens=60))
            except Exception:
                log.exception("срок покупки %s не разобран", p["id"])
                return
            await db.execute("UPDATE crm.purchases SET product=$2, months=$3, expires_at=$4, checked_at=now() "
                             "WHERE id=$1", p["id"], product, months,
                             add_months(p["paid_at"], months) if months else None)
            done += 1
            if done % 100 == 0:
                log.info("сроки: %d/%d", done, len(todo))
    await asyncio.gather(*(one(p) for p in todo))
    return done


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LABEL = re.compile(r"(?i)^\W*(?:(?:парол\w*|password|pass)(?=\W))?\W*")   # разделители, эмодзи, «Пароль:»
PASSWORD = re.compile(r"(?=[^\s]*\d)[A-Za-z0-9!#$%^&*()_+=.\-]{6,40}")    # латиница/цифры, хотя бы одна цифра
NOT_CREDS = re.compile(r"(?i)активации ключа|mailto:")   # инструкции по ключам игр — там почта клиента
CREDS_BEFORE, CREDS_AFTER = timedelta(days=2), timedelta(days=7)   # где искать учётку относительно покупки


def parse_creds(text: str | None) -> tuple[str, str | None, str | None] | None:
    """(логин, пароль, резервная почта) из сообщения Макса: «почта:пароль», «почта ⏎ пароль», «Почта: … Пароль: …»,
    вторая почта — резервная. Нет почты — None."""
    if not text or NOT_CREDS.search(text):
        return None
    emails = [e.rstrip(".") for e in EMAIL.findall(text)]
    if not emails:
        return None
    login = emails[0]
    rest = LABEL.sub("", text[text.index(login) + len(login):], count=1)
    m = PASSWORD.match(rest)
    password = m.group(0) if m and (len(rest) == m.end() or not re.match(r"[\w@]", rest[m.end()])) else None
    return login, password, next((e for e in emails[1:] if e.lower() != login.lower()), None)


async def fill_creds(db: asyncpg.Pool) -> int:
    """Учётка к покупке: сообщение Макса с почтой в чате покупки, ближайшее к оплате (за 2 дня до — 7 дней после).
    Свежие покупки без учётки перепроверяем, пока не прошло 7 дней."""
    todo = await db.fetch("SELECT id, chat_id, paid_at FROM crm.purchases WHERE creds_checked_at IS NULL "
                          "AND coalesce(product, '?') NOT IN ('none', 'other')")
    found = 0
    for p in todo:
        rows = await db.fetch("SELECT text, created_at FROM gateway.messages WHERE chat_id=$1 AND origin='owner' "
                              "AND created_at BETWEEN $2::timestamptz - $3::interval AND $2::timestamptz + $4::interval "
                              "AND text ~ '@'", p["chat_id"], p["paid_at"], CREDS_BEFORE, CREDS_AFTER)
        best = min(((abs(r["created_at"] - p["paid_at"]), c) for r in rows if (c := parse_creds(r["text"]))),
                   default=None, key=lambda x: x[0])
        if best:
            found += 1
            await db.execute("UPDATE crm.purchases SET login=$2, password=$3, recovery_email=$4, creds_checked_at=now() "
                             "WHERE id=$1", p["id"], *best[1])
        elif datetime.now(p["paid_at"].tzinfo) - p["paid_at"] > CREDS_AFTER:
            await db.execute("UPDATE crm.purchases SET creds_checked_at=now() WHERE id=$1", p["id"])
    return found


async def main() -> None:
    from shared.db import connect
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db, llm = await connect(SCHEMA), LLM()
    try:
        print("новых покупок:", await sync(db))
        print("разобрано сроков:", await fill_terms(db, llm))
        print("найдено учёток:", await fill_creds(db))
    finally:
        await llm.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
