"""Ручное управление, пока нет VK-бота.

python -m services.avito_gateway.ctl status
python -m services.avito_gateway.ctl start | stop
python -m services.avito_gateway.ctl send <chat_id> "текст"
"""
import asyncio
import sys
from datetime import datetime, timezone

from services.avito_gateway.main import SCHEMA
from shared.db import connect


async def main(cmd: str, *args: str) -> None:
    db = await connect(SCHEMA)
    try:
        if cmd == "start":
            await db.execute("UPDATE gateway.accounts SET started_at=$1", datetime.now(timezone.utc))
        elif cmd == "stop":
            await db.execute("UPDATE gateway.accounts SET started_at=NULL")
        elif cmd == "send":
            chat_id, text = args
            await db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) "
                             "SELECT user_id, $1, $2 FROM gateway.accounts LIMIT 1", chat_id, text)
        for r in await db.fetch("SELECT user_id, name, started_at, all_chats, whitelist FROM gateway.accounts"):
            print(dict(r))
        for r in await db.fetch("SELECT id, chat_id, kind, payload->>'text' AS text, processed_at "
                                "FROM gateway.events ORDER BY id DESC LIMIT 5"):
            print(dict(r))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
