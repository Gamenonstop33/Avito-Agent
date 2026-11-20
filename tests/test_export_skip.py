"""export --all: чат, который Avito не отдаёт (402), пропускается, остальные выгружаются (18.11, XBOXGARANT)."""
import asyncio

import pytest

from services.avito_gateway import export
from shared.avito import AvitoAuthError, AvitoError


class FakeAvito:
    def __init__(self, bad: dict):
        self.bad = bad   # chat_id → исключение

    async def messages(self, me, chat_id, limit=100, offset=0):
        if chat_id in self.bad:
            raise self.bad[chat_id]
        return [{"id": f"{chat_id}-1", "created": 1, "author_id": 7, "type": "text", "content": {"text": "привет"}}]


def chat(cid, title="Подписка"):
    return {"id": cid, "updated": 5, "context": {"value": {"id": 1, "title": title}}}


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(export, "PAUSE", 0)


def test_skips_unavailable_chat():
    av = FakeAvito({"b": AvitoError("GET …: 402 Перейдите на подписку с API мессенджера", 402)})
    done = {}
    skipped = asyncio.run(export.fetch_chats(av, 1, [chat("a"), chat("b", "Курьер"), chat("c")], done))
    assert set(done) == {"a", "c"}
    assert skipped == ["b (Курьер: 402)"]
    assert done["a"]["messages"][0]["role"] == "client"


def test_auth_error_still_fails():
    av = FakeAvito({"b": AvitoAuthError("нет доступа")})
    with pytest.raises(AvitoAuthError):
        asyncio.run(export.fetch_chats(av, 1, [chat("a"), chat("b")], {}))
