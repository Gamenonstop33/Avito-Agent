"""База клиентов: кто уже покупал (для скидки новым, напоминаний, рассылок)."""
import re
from datetime import datetime

import asyncpg

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS crm;
CREATE TABLE IF NOT EXISTS crm.customers (
    avito_user_id   bigint PRIMARY KEY,
    name            text,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    purchased_at    timestamptz,          -- NULL = ещё ничего не покупал (переписка покупкой не считается)
    purchase_source text                  -- history | chat | owner
);
"""

# Эти шаблоны Макс шлёт только после оплаты — значит, покупка была
PURCHASE_MARKERS = re.compile(
    r"(?i)нажмите кнопку xbox на джойстике|оставьте отзыв|пишите любой тег|без ограничений\s+пропустить"
    r"|переключаемся на свой аккаунт|выходим из (моего|доп)"
    # игры/ключи Xbox и Steam: инструкция после оплаты, рекомендации после смены региона, просьба об отзыве
    r"|инструкция для активации ключа|по вопросам активации или же успешной активации"
    r"|рекомендации❗\s*24 часа|помните про рекомендации|благодар\w*.{0,25}отзыв")


def is_purchase_msg(text: str | None) -> bool:
    return bool(text and PURCHASE_MARKERS.search(text))


async def touch(db: asyncpg.Pool, user_id: int, name: str | None) -> None:
    await db.execute("INSERT INTO crm.customers (avito_user_id, name) VALUES ($1,$2) ON CONFLICT (avito_user_id) "
                     "DO UPDATE SET name=COALESCE(EXCLUDED.name, crm.customers.name)", user_id, name)


async def mark_purchased(db: asyncpg.Pool, user_id: int, at: datetime, source: str) -> None:
    await db.execute("INSERT INTO crm.customers (avito_user_id, purchased_at, purchase_source) VALUES ($1,$2,$3) "
                     "ON CONFLICT (avito_user_id) DO UPDATE SET purchased_at=LEAST(crm.customers.purchased_at, $2), "
                     "purchase_source=COALESCE(crm.customers.purchase_source, $3)", user_id, at, source)


async def is_new(db: asyncpg.Pool, user_id: int | None) -> bool:
    """Новый = ни разу не покупал. Неизвестного клиента считаем новым."""
    if user_id is None:
        return True
    return await db.fetchval("SELECT purchased_at FROM crm.customers WHERE avito_user_id=$1", user_id) is None
