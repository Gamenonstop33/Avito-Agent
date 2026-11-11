"""Резервный ключ нейросети (28.10): 402 на основном — тот же запрос резервным, уведомление один раз;
основной пополнили — бот возвращается на него сам."""
import asyncio
import time

import httpx
import pytest

import shared.llm as llm_mod
from shared.llm import LLM

MAIN, RESERVE = "sk-main00000000001", "sk-reserve00000002"


def make(monkeypatch, reserve: str = RESERVE):
    monkeypatch.setenv("LLM_API_KEY", MAIN)
    monkeypatch.setenv("LLM_API_KEY_RESERVE", reserve)
    monkeypatch.setattr(llm_mod, "_reserve_at", 0.0)
    monkeypatch.setattr(llm_mod, "_main_ok", False)
    state = {"money": False, "calls": []}
    sent = []

    async def key_state(value, text):
        sent.append(value)

    async def alarm(e):
        pass

    monkeypatch.setattr(llm_mod, "_key_state", key_state)
    monkeypatch.setattr(llm_mod, "alarm", alarm)

    def handler(req: httpx.Request) -> httpx.Response:
        key = req.headers["Authorization"].removeprefix("Bearer ")
        if req.url.path == "/user/balance":
            total = "5.00" if state["money"] else "0.00"
            return httpx.Response(200, json={"is_available": state["money"],
                                             "balance_infos": [{"currency": "USD", "total_balance": total}]})
        state["calls"].append(key)
        if key == MAIN and not state["money"]:
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": key}}], "usage": {}})

    llm = LLM(base_url="https://api.test", model="m", tries=1)
    llm._http = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return llm, state, sent


def ask(llm):
    return asyncio.run(llm.chat([{"role": "user", "content": "привет"}]))


def test_switch_to_reserve_once(monkeypatch):
    llm, state, sent = make(monkeypatch)
    assert ask(llm) == RESERVE
    assert ask(llm) == RESERVE
    assert state["calls"] == [MAIN, RESERVE, RESERVE]   # пока ждём пополнения — основной не дёргаем
    assert sent == ["reserve"] and llm_mod.on_reserve()


def test_back_to_main_after_topup(monkeypatch):
    llm, state, sent = make(monkeypatch)
    ask(llm)
    state["money"] = True
    assert ask(llm) == RESERVE   # баланс основного смотрим не чаще MAIN_CHECK
    llm_mod._reserve_at = time.monotonic() - llm_mod.MAIN_CHECK - 1
    assert ask(llm) == MAIN
    assert sent == ["reserve", "main"] and not llm_mod.on_reserve()


def test_main_works(monkeypatch):
    llm, state, sent = make(monkeypatch)
    state["money"] = True
    assert ask(llm) == MAIN and ask(llm) == MAIN
    assert sent == ["main"]   # отметка «основной работает» — один раз на процесс (в БД пишет только после резерва)


def test_no_reserve(monkeypatch):
    llm, state, sent = make(monkeypatch, reserve="")
    with pytest.raises(httpx.HTTPStatusError):
        ask(llm)
    assert sent == [] and not llm_mod.on_reserve()


def test_second_account_silent(monkeypatch):
    """Копия второго аккаунта (DB_NS=2) ключ меняет сама, но в VK не пишет — пишет основная (29.10)."""
    async def no_db():
        raise AssertionError("копия второго аккаунта не должна писать о ключе")
    monkeypatch.setattr(llm_mod, "NS", "2")
    monkeypatch.setattr(llm_mod, "connect_one", no_db)
    asyncio.run(llm_mod._key_state("reserve", "перешли на резервный"))
