"""Отказ VK на messages.send с peer_ids приходит полем error в ответе — это должна быть ошибка, а не тишина (20.11)."""
import asyncio

import pytest

from shared import alerts
from shared.vk import VK, VKError

REFUSED = [{"peer_id": 1, "error": {"code": 901, "description": "Can't send messages for users without permission"}}]
OK = [{"peer_id": 1, "message_id": 5, "conversation_message_id": 7}]


def _vk(answer):
    vk = object.__new__(VK)
    vk.on_sent = None

    async def call(method, **params):
        return answer
    vk.call = call
    return vk


def test_send_refused_raises():
    with pytest.raises(VKError) as e:
        asyncio.run(_vk(REFUSED).send(1, "привет"))
    assert e.value.code == 901


def test_send_ok_returns_conv_id():
    assert asyncio.run(_vk(OK).send(1, "привет")) == 7


def test_notify_admins_logs_refusal_and_goes_on(monkeypatch, caplog):
    sent = []

    class FakeVK:
        def __init__(self, *a):
            pass

        async def send(self, uid, text):
            if uid == 1:
                raise VKError("messages.send: нет разрешения (код 901)", 901)
            sent.append(uid)

        async def close(self):
            pass
    monkeypatch.setattr(alerts, "VK", FakeVK)
    monkeypatch.setattr(alerts, "roles", lambda: {1: "admin", 2: "admin", 3: "owner"})
    monkeypatch.setattr(alerts, "env", lambda k: "1")
    asyncio.run(alerts.notify_admins("тест"))
    assert sent == [2]
    assert "сообщения сообществу запрещены" in caplog.text
