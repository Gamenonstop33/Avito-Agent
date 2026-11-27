"""Оплата по СБП QR через Platega (25.11): клиент API, режим «⚡ Только Platega», новая ссылка вместо истёкшей,
опрос статуса."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

from services.dialog import worker
from shared import platega, sales


def client(handler):
    http = httpx.AsyncClient(base_url="https://app.test", transport=httpx.MockTransport(handler))
    return platega.PlategaClient("m-1", "s-1", http=http)


def test_create_nspk_link_and_body():
    seen = []

    def handler(req):
        seen.append(req)
        assert req.headers["X-MerchantId"] == "m-1" and req.headers["X-Secret"] == "s-1"
        if req.method == "POST":
            return httpx.Response(200, json={"transactionId": "t-1", "redirect": "https://pay.platega.io?x",
                                             "status": "PENDING", "expiresIn": "00:15:00"})
        assert req.url.path == "/h2h/t-1"
        return httpx.Response(200, json={"amount": 990, "qr": "https://qr.nspk.ru/AB?sum=99000"})

    t = asyncio.run(client(handler).create("p77-1", 990, "Оплата заказа №77", 555, "u2i-x"))
    assert t == {"id": "t-1", "url": "https://qr.nspk.ru/AB?sum=99000", "ttl": timedelta(minutes=15)}
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/transaction/process"
    assert body["paymentMethod"] == 2 and body["paymentDetails"] == {"amount": 990, "currency": "RUB"}
    assert body["payload"] == body["orderId"] == "p77-1"
    assert body["metadata"] == {"user_email": "555@mail.ru", "userId": "555"}   # один покупатель — один адрес


def test_create_falls_back_to_payform_without_h2h():
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"transactionId": "t-2", "redirect": "https://pay.platega.io?y",
                                             "status": "PENDING"})
        return httpx.Response(403, text="H2H disabled")

    t = asyncio.run(client(handler).create("p1-1", 500, "x", None, "u2i-x"))
    assert t["url"] == "https://pay.platega.io?y" and t["ttl"] == platega.TTL


def test_errors():
    def handler(req):
        return httpx.Response(401, text="bad pair")

    try:
        asyncio.run(client(handler).status("t"))
        raise AssertionError("ждали PlategaError")
    except platega.PlategaError as e:
        assert e.status == 401


def test_lifetime_and_email():
    assert platega.lifetime("01:02:03") == timedelta(hours=1, minutes=2, seconds=3)
    assert platega.lifetime(None) == platega.lifetime("мусор") == platega.TTL
    assert platega.buyer_email(None, "u2i-a") == platega.buyer_email(None, "u2i-a") != platega.buyer_email(None, "u2i-b")


def test_platega_mode(monkeypatch):
    monkeypatch.setattr(sales, "PLATEGA_READY", True)
    monkeypatch.setattr(sales, "ALFA_READY", False)
    monkeypatch.setattr(sales, "ALFA_TEST_CHATS", {"u2i-test"})
    assert sales.gate("platega") == "platega" and sales.gate("ip") == sales.gate("mix") == "alfa"
    assert sales.ip_mode(1, "u2i-x", "platega") == "only"
    assert sales.ip_mode(1, "u2i-test", "platega") == "test"
    assert sales.ip_mode(1, "u2i-x", "ip") is None              # Альфы нет — её режим не работает
    assert sales.pay_method(20, 0, 16, sales.ip_mode(1, "u2i-x", "platega")) == "ip"
    assert "platega" in sales.shown_modes("card") and "✅ ⚡ Только Platega" in sales.pay_mode_text("platega")
    monkeypatch.setattr(sales, "PLATEGA_READY", False)
    assert sales.ip_mode(1, "u2i-x", "platega") is None         # ключей нет — только карты
    assert "platega" not in sales.shown_modes("card")            # и кнопки режима нет (магазин Димана)
    assert "Ключи Platega не заданы" in sales.pay_mode_text("platega")


class Gate:
    def __init__(self, status):
        self.status_value, self.created = status, 0

    async def status(self, _):
        return {"status": self.status_value}

    async def create(self, number, rub, *_):
        self.created += 1
        return {"id": f"new-{self.created}", "url": "https://qr.nspk.ru/NEW", "ttl": timedelta(minutes=15)}


class DB:
    def __init__(self):
        self.updates = []

    async def fetchrow(self, sql, *args):
        self.updates.append(args)
        return {"id": args[0], "pay_id": args[1], "pay_url": args[2], "pay_until": args[3]}


def order(**kw):
    o = {"id": 7, "chat_id": "u2i-x", "client_id": 5, "amount": 990, "method": "ip", "pay_gate": "platega",
         "status": "awaiting", "pay_id": "old", "pay_until": datetime.now(timezone.utc) - timedelta(minutes=1)}
    return {**o, **kw}


def dialog(status):
    d = worker.Dialog.__new__(worker.Dialog)
    d.db, d.platega, d.alfa = DB(), Gate(status), None
    return d


def test_fresh_link_only_when_expired_and_cancelled():
    d = dialog(platega.CANCELED)
    new = asyncio.run(d.fresh_link(order()))
    assert new["pay_id"] == "new-1" and new["pay_url"] == "https://qr.nspk.ru/NEW"
    for kw in ({"pay_until": datetime.now(timezone.utc) + timedelta(minutes=5)},   # ещё живая
               {"status": "checking"}, {"pay_gate": "alfa"}, {"method": "card"}):
        d = dialog(platega.CANCELED)
        assert asyncio.run(d.fresh_link(order(**kw)))["pay_id"] == "old" and not d.platega.created
    for status in (platega.CONFIRMED, platega.PENDING):   # оплатил по старой / ещё идёт — ссылку не меняем
        d = dialog(status)
        assert asyncio.run(d.fresh_link(order()))["pay_id"] == "old" and not d.platega.created


def test_gate_paid():
    assert asyncio.run(dialog(platega.CONFIRMED).gate_paid(order())) is True
    assert asyncio.run(dialog(platega.PENDING).gate_paid(order())) is False
    d = dialog(platega.CONFIRMED)
    d.platega = None
    assert asyncio.run(d.gate_paid(order())) is None   # ключи убрали — заказ не трогаем
