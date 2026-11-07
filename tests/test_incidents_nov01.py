"""Регрессии из переписки с Максом 01.11; без внешних API и отправки сообщений."""
import asyncio
import sqlite3

import httpx
import pytest

from shared.avito import AvitoClient, AvitoError, AvitoAuthError
from shared import sales
from services.dialog import brain


@pytest.mark.parametrize("status,message,refresh", [(403, "invalid access token", True),
    (401, "expired", True), (403, "access denied", False)])
def test_token_refresh(status, message, refresh):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        if calls.count("/chats") == 1:
            return httpx.Response(status, json={"result": {"message": message}})
        return httpx.Response(200, json={"ok": True})
    async def run():
        async with httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handler)) as http:
            client = AvitoClient("id", "secret", http)
            if refresh:
                assert await client._req("GET", "/chats") == {"ok": True}
            else:
                with pytest.raises(AvitoError):
                    await client._req("GET", "/chats")
    asyncio.run(run())
    assert calls.count("/token") == (2 if refresh else 1)


def test_invalid_new_token_is_not_retried_forever():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "bad", "expires_in": 3600})
        return httpx.Response(403, json={"result": {"message": "invalid access token"}})
    async def run():
        async with httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(AvitoError):
                await AvitoClient("id", "secret", http)._req("GET", "/chats")
    asyncio.run(run())
    assert calls == ["/token", "/chats", "/token", "/chats"]


def test_unauthorized_credentials_have_safe_error():
    async def run():
        async with httpx.AsyncClient(base_url="https://test", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"error": "unauthorized_client"}))) as http:
            with pytest.raises(AvitoAuthError, match="ключи приложения не авторизованы"):
                await AvitoClient("id", "secret", http)._get_token()
    asyncio.run(run())


def test_actual_price_question_and_renewal():
    price = "🎮 Консоль\n2 мес — 1 090₽\n4 мес — 1 790₽"
    for question in ["Здравствуйте! Сколько подписка на Xbox стоит?", "Сначала хочу узнать цены и решить", "А где прайс?"]:
        reply = brain.ensure_price("Сначала посмотрите прайс", question, price)
        assert price in brain.render_reply(reply, price)
    assert not brain.price_request("Сколько стоит GTA 5?")
    assert brain.ensure_price("Вот актуальный прайс:", "Здравствуйте", price).endswith("{прайс}")


def test_review_is_not_payment_and_order_uses_last_choice():
    history = [("client", "Подписку на Xbox Series S делаете?"),
        ("seller", "Для новых клиентов скидка за отзыв"), ("client", "Пока на 1 месяц"),
        ("client", "А, на 2"), ("seller", "На консоли 2 месяца — 1090 ₽, со скидкой — 990 ₽"),
        ("client", "Хорошо, напишу отзыв"), ("seller", "Хорошо, пишите"), ("client", "Написал")]
    assert brain.review_only("Написал", history)
    assert brain.review_only("Только пишет что опубликуется после проверки", history)
    assert not brain.review_only("Отзыв написал и оплатил", history)
    assert brain.review_order(history, {"console": {2: 1090}, "pc": {1: 400}}) == {
        "product": "console", "months": 2, "price": 1090}
    assert brain.review_order([("client", "Написал отзыв")], {"console": {2: 1090}}) is None
    assert brain.review_order(history + [("client", "Думаю на 8–10 месяцев"), ("client", "Написал отзыв")],
                              {"console": {2: 1090, 8: 2890, 10: 3590}}) is None


def test_stock_query_preserves_slot_and_fifo():
    # Выполняем условия реального PICK на временной таблице; адаптируем только синтаксис PostgreSQL.
    con = sqlite3.connect(":memory:")
    con.execute("ATTACH DATABASE ':memory:' AS sales")
    con.execute("CREATE TABLE sales.accounts(id, product, status, months, expires_at, email, password, slot1_client, slot2_client, created_at)")
    con.executemany("INSERT INTO sales.accounts VALUES (?, 'console', 'free', 2, NULL, 'e', 'p', ?, ?, ?)",
                    [(3, None, 'busy', '2026-10-02'), (2, None, None, '2026-10-01'), (1, 'busy', None, '2026-10-01')])
    query = sales.PICK.replace("::int", "").replace("now()", "CURRENT_TIMESTAMP")
    args = {"1": "console", "2": 2, "3": None, "4": False}
    assert con.execute(query, args).fetchone()[0] == 1
    args["3"] = 1
    assert con.execute(query, args).fetchone()[0] == 2
    args.update({"3": 2, "4": True})
    assert con.execute(query, args).fetchone() is None  # обещали основное место, а привычное — второе: Максу
    args["4"] = False
    con.execute("UPDATE sales.accounts SET slot2_client='busy'")
    args["3"] = 2
    assert con.execute(query, args).fetchone() is None
    con.close()


def test_free_slot_is_strict_and_new_clients_get_first():
    stock = {"product": "console", "slot1_client": None, "slot2_client": "busy"}
    assert sales.free_slot(stock, None) == 1
    with pytest.raises(ValueError):
        sales.free_slot(stock, 2)


def test_setup_steps_are_independent_of_slot():
    for slot in (1, 2):
        text = sales.instruction(slot)
        assert "Если появится создание игрового тега" in text
        assert "Если этих экранов нет" in text
        assert ("сделать домашней" in text) == (slot == 1)
    assert "Использовать пароль" not in sales.CREDS_FIRST + sales.CREDS_SECOND
