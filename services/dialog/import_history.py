"""Загружает всю историю из выгрузки в БД: чаты и сообщения (gateway.*), клиенты и покупки (crm.customers).

Покупка = в чате Макс отправлял шаблон, который шлёт только после оплаты (инструкция подключения, «оставьте отзыв»).
Запуск: python -m services.dialog.import_history   (берёт dialogs_all.jsonl, если есть, иначе dialogs.jsonl)
"""
import asyncio
import json
from datetime import datetime, timezone

from services.avito_gateway.export import list_all_chats
from services.avito_gateway.logic import DELETED_TEXT
from services.avito_gateway.main import SCHEMA as GATEWAY_SCHEMA
from shared import crm
from shared.avito import AvitoClient
from shared.config import DATA_DIR, env
from shared.db import connect


async def main() -> None:
    av = AvitoClient(env("AVITO_CLIENT_ID"), env("AVITO_CLIENT_SECRET"))
    db = await connect(GATEWAY_SCHEMA + crm.SCHEMA)
    try:
        me = (await av.me())["id"]
        clients: dict[str, dict] = {}
        for c in await list_all_chats(av, me):
            u = next((u for u in c.get("users", []) if u.get("id") != me), None)
            if u:
                clients[c["id"]] = u

        # ответы бота в выгрузке — от того же аккаунта, что и Макс: отличаем по id отправленных из outbox
        bot_ids = {r["message_id"] for r in await db.fetch("SELECT message_id FROM gateway.outbox WHERE message_id IS NOT NULL")}
        now = datetime.now(timezone.utc)
        seen = bought = msgs = 0
        src = DATA_DIR / "export" / "dialogs_all.jsonl"
        src = src if src.exists() else DATA_DIR / "export" / "dialogs.jsonl"
        for line in filter(None, src.read_text(encoding="utf-8").split("\n")):  # не splitlines: U+2028 в тексте
            d = json.loads(line)
            u = clients.get(d["chat_id"]) or {}
            await db.execute(
                "INSERT INTO gateway.chats (id, account_id, item_title, client_name, client_id) VALUES ($1,$2,$3,$4,$5) "
                "ON CONFLICT (id) DO UPDATE SET item_title=COALESCE(gateway.chats.item_title, EXCLUDED.item_title), "
                "client_name=COALESCE(gateway.chats.client_name, EXCLUDED.client_name), "
                "client_id=COALESCE(gateway.chats.client_id, EXCLUDED.client_id)",
                d["chat_id"], me, d.get("item_title"), u.get("name"), u.get("id"))
            rows = [(m["id"], d["chat_id"], "out" if m["role"] == "seller" else "in",
                     "bot" if m["id"] in bot_ids else {"seller": "owner", "client": "client"}.get(m["role"], "system"),
                     m["type"] or "text", m["text"], json.dumps({"text": m["text"]}, ensure_ascii=False),
                     datetime.fromtimestamp(m["created"], timezone.utc),
                     now if m["text"] == DELETED_TEXT else None) for m in d["messages"]]
            await db.executemany(  # история: без событий для dialog, бот на неё не отвечает
                "INSERT INTO gateway.messages (id, chat_id, direction, origin, type, text, content, created_at, "
                "deleted_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (id) DO UPDATE SET "   # удалили позже
                "deleted_at=COALESCE(gateway.messages.deleted_at, EXCLUDED.deleted_at)", rows)
            msgs += len(rows)
            if not u:
                continue
            await crm.touch(db, u["id"], u.get("name"))
            seen += 1
            sale = next((m for m in d["messages"] if m["role"] == "seller" and crm.is_purchase_msg(m["text"])), None)
            if sale:
                await crm.mark_purchased(db, u["id"], datetime.fromtimestamp(sale["created"], timezone.utc), "history")
                bought += 1
        print(f"Сообщений загружено: {msgs}")
        total = await db.fetchval("SELECT count(*) FILTER (WHERE purchased_at IS NOT NULL) FROM crm.customers")
        print(f"Чатов с известным клиентом: {seen}, с покупкой: {bought}. Всего покупателей в базе: {total}")
    finally:
        await av.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
