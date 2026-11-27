import asyncio

import httpx

from services.dialog.brain import guard_reply, lessons_block
from shared.avito import AvitoClient, AvitoError


def test_reply_never_carries_credentials():
    r = guard_reply("Входите: donor7@outlook.com:Qwe12345. Потом напишите сюда.", True)
    assert "outlook" not in r and "Qwe12345" not in r and "напишите" in r
    assert "+7" not in guard_reply("Звоните +7 912 345-67-89. Ответим.", True)


def test_lessons_without_credentials():
    rows = [{"reason": "code", "summary": "вход", "client_text": "Пароль просит", "owner_answer": "vh2867k@outlook.com:7RKpSngt"},
            {"reason": "game", "summary": "цена", "client_text": "Сколько атомик? мой a@b.ru", "owner_answer": "2300"}]
    block = lessons_block(rows)
    assert "7RKpSngt" not in block and "outlook" not in block and "a@b.ru" not in block and "2300" in block


def client(handler) -> AvitoClient:
    av = AvitoClient("id", "secret", http=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
    av._token, av._token_exp = "t", 1e12
    return av


def test_avito_get_retries_network_errors(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))   # без ожиданий
    calls = []

    def handler(req):
        calls.append(req.method)
        if len(calls) < 3:
            raise httpx.ReadTimeout("t", request=req)
        return httpx.Response(200, json={"chats": []})
    assert asyncio.run(client(handler).chats(1)) == [] and len(calls) == 3


def test_avito_post_timeout_not_blindly_resent(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("t", request=req)   # могло дойти — повторять вслепую нельзя (дубль клиенту)
    try:
        asyncio.run(client(handler).send_text(1, "c", "hi"))
        assert False
    except AvitoError as e:
        assert e.status is None and len(calls) == 1


def test_avito_4xx_has_status():
    try:
        asyncio.run(client(lambda req: httpx.Response(403, text="forbidden")).send_text(1, "c", "hi"))
        assert False
    except AvitoError as e:
        assert e.status == 403
