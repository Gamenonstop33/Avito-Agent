"""Приём (18.11): Avito отдаёт 500 на один чат — остальные чаты всё равно читаются (прод 17.11: новый клиент 2 мин без
ответа, пока цикл падал на соседних чатах); ключи или все чаты сразу — ошибка цикла, как раньше (алерт)."""
import asyncio
from datetime import datetime, timezone

import pytest

from services.avito_gateway.worker import Gateway
from shared.avito import AvitoAuthError, AvitoError

T0 = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)


class Pool:
    async def fetchrow(self, *_):
        return {"all_chats": True, "whitelist": [], "started_at": T0, "stopped_at": None}

    async def fetch(self, *_):
        return []   # ни одно последнее сообщение ещё не известно, игнора нет


class Avito:
    async def chats(self, *_, **__):
        return [{"id": c, "last_message": {"id": f"m-{c}"}} for c in ("bad1", "new", "bad2")]


def gateway(bad, err=AvitoError):
    gw, synced = Gateway(Pool(), Avito(), 1), []

    async def sync(chat, *_):
        if chat["id"] in bad:
            raise err(f"GET {chat['id']}: не удалось после повторов (500)")
        synced.append(chat["id"])

    gw._sync_chat = sync
    return gw, synced


def test_one_failing_chat_does_not_block_others():
    gw, synced = gateway({"bad1", "bad2"})
    asyncio.run(gw.poll())
    assert synced == ["new"]


def test_all_chats_failing_is_cycle_error():
    gw, _ = gateway({"bad1", "new", "bad2"})
    with pytest.raises(AvitoError):
        asyncio.run(gw.poll())


def test_auth_error_stops_cycle():
    gw, synced = gateway({"bad1"}, AvitoAuthError)
    with pytest.raises(AvitoAuthError):
        asyncio.run(gw.poll())
    assert synced == []
