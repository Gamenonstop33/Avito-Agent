"""Разово (30.10): Steam не продаём — покупки в чатах объявлений про Steam больше не покупки.

Удаляет из crm.purchases покупки в таких чатах; клиенту, у которого были такие покупки, «покупал с» пересчитывается
по оставшимся (нет других — снова новый: скидка за отзыв и подарок, рассылки «не завершили покупку»).
Дальше так и считается само (shared.kb_classes.steam_item в import_history, worker, broadcast.purchases).
Запуск в каждой копии (DB_NS — свой аккаунт): python -m services.dialog.drop_steam [--dry]
"""
import argparse
import asyncio

from shared.db import connect
from shared.kb_classes import steam_item


async def main(dry: bool) -> None:
    db = await connect()
    try:
        chats = [r["id"] for r in await db.fetch("SELECT id, item_title FROM gateway.chats WHERE item_title IS NOT NULL")
                 if steam_item(r["item_title"])]
        rows = await db.fetch("SELECT id, client_id FROM crm.purchases WHERE chat_id = ANY($1)", chats)
        clients = sorted({r["client_id"] for r in rows if r["client_id"]})
        # клиент снова новый, если других покупок нет (переписка покупкой не считается)
        alone = await db.fetchval(
            "SELECT count(*) FROM unnest($1::bigint[]) c(id) WHERE NOT EXISTS (SELECT 1 FROM crm.purchases p "
            "WHERE p.client_id = c.id AND NOT p.id = ANY($2::bigint[]))", clients, [r["id"] for r in rows])
        print(f"чатов про Steam: {len(chats)}, покупок в них: {len(rows)}, клиентов: {len(clients)}, "
              f"из них снова новые: {alone}" + (" (--dry: ничего не изменено)" if dry else ""))
        if dry or not rows:
            return
        async with db.acquire() as con, con.transaction():
            await con.execute("DELETE FROM crm.purchases WHERE id = ANY($1::bigint[])", [r["id"] for r in rows])
            await con.execute(
                "UPDATE crm.customers c SET purchased_at = (SELECT min(p.paid_at) FROM crm.purchases p "
                "WHERE p.client_id = c.avito_user_id), purchase_source = CASE WHEN EXISTS (SELECT 1 FROM crm.purchases p "
                "WHERE p.client_id = c.avito_user_id) THEN c.purchase_source END WHERE c.avito_user_id = ANY($1::bigint[])",
                clients)
    finally:
        await db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry", action="store_true", help="только посчитать")
    asyncio.run(main(p.parse_args().dry))
