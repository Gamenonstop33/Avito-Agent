"""03.11: сбой DeepSeek — после алерта пробный запрос раз в минуту, ожил — одно сообщение в группу."""
import asyncio
from datetime import datetime, timezone

import httpx

from shared import llm as L


def _status(code):
    req = httpx.Request("POST", "https://api.test/chat/completions")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


def test_provider_down():
    for e in (_status(503), _status(500), _status(429), _status(402), _status(401), httpx.ReadTimeout("t"),
              httpx.ConnectError("c"), TimeoutError()):
        assert L.provider_down(e), e
    for e in (_status(400), _status(422), RuntimeError("пустой ответ")):
        assert not L.provider_down(e), e


def test_alarm_starts_one_watch(monkeypatch):
    calls = {"down": 0, "watch": 0, "admins": []}

    async def mark_down():
        calls["down"] += 1

    async def watch():
        calls["watch"] += 1
        await asyncio.sleep(10)

    async def admins(text):
        calls["admins"].append(text)

    import shared.alerts as alerts
    monkeypatch.setattr(L, "_mark_down", mark_down)
    monkeypatch.setattr(L, "watch_up", watch)
    monkeypatch.setattr(alerts, "notify_admins", admins)
    monkeypatch.setattr(L, "_watch", None)
    monkeypatch.setattr(L, "_alarm_at", float("-inf"))   # 0.0 — тест падал первый час после загрузки (19.11)

    async def run():
        await L.alarm(_status(503))
        await L.alarm(_status(503))   # второй сбой: наблюдение уже идёт, алерт — не чаще раза в час
        await asyncio.sleep(0)
        L._watch.cancel()
        await L.alarm(_status(400))   # ошибка нашего запроса — не пингуем

    asyncio.run(run())
    assert calls["down"] == 1 and calls["watch"] == 1 and len(calls["admins"]) == 1
    assert "напишу в группу" in calls["admins"][0]


def test_watch_up_pings_until_alive(monkeypatch):
    since = datetime(2026, 11, 3, 9, 4, tzinfo=timezone.utc)
    state = {"pings": 0, "said": []}

    async def fake_chat(self, messages, json_mode, temperature, max_tokens):
        state["pings"] += 1
        assert max_tokens == 1
        if state["pings"] < 3:
            raise _status(503)
        return "ok"

    async def mark_up():
        return since, False

    async def say(s, backup=False):
        state["said"].append(s)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(L.LLM, "_chat", fake_chat)
    monkeypatch.setattr(L, "_mark_up", mark_up)
    monkeypatch.setattr(L, "_say_up", say)
    monkeypatch.setattr(L.asyncio, "sleep", no_sleep)
    asyncio.run(L.watch_up())
    assert state["pings"] == 3 and state["said"] == [since]


def test_watch_up_silent_if_other_process_announced(monkeypatch):
    said = []

    async def ok(self, *a):
        return "ok"

    async def mark_up():
        return None   # отметку уже снял другой процесс

    async def say(s):
        said.append(s)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(L.LLM, "_chat", ok)
    monkeypatch.setattr(L, "_mark_up", mark_up)
    monkeypatch.setattr(L, "_say_up", say)
    monkeypatch.setattr(L.asyncio, "sleep", no_sleep)
    asyncio.run(L.watch_up())
    assert said == []


def _backup_llm(monkeypatch):
    monkeypatch.setenv("LLM_BACKUP_KEY", "test-key")
    monkeypatch.setattr(L, "BACKUP_FOLDER", "b1gfolder")
    monkeypatch.setattr(L, "_watch", None)
    monkeypatch.setattr(L, "_backup_said", False)
    return L.LLM(base_url="https://api.test", api_key="k", timeout=5, tries=1, backup=True)


def _patch_backup(monkeypatch, state, backup_fails=False):
    async def ask(self, body, key, http=None):
        if http is None:
            state["main"] += 1
            raise _status(503)
        state["backup"].append(body["model"])
        if backup_fails:
            raise _status(401)
        return "из резерва"

    async def watch(e):
        state["watch"] += 1

    async def say(e):
        state["said"] += 1

    async def alarm(e, backup=None):
        state["alarm"].append(backup)

    monkeypatch.setattr(L.LLM, "_ask", ask)
    monkeypatch.setattr(L, "watch", watch)
    monkeypatch.setattr(L, "_say_backup", say)
    monkeypatch.setattr(L, "alarm", alarm)


def test_backup_answers_when_deepseek_down(monkeypatch):
    llm = _backup_llm(monkeypatch)
    state = {"main": 0, "backup": [], "watch": 0, "said": 0, "alarm": []}
    _patch_backup(monkeypatch, state)

    async def run():
        a = await llm.chat([{"role": "user", "content": "привет"}])
        b = await llm.chat([{"role": "user", "content": "ещё"}])
        await llm.close()
        return a, b

    assert asyncio.run(run()) == ("из резерва", "из резерва")
    # DeepSeek спросили оба раза (watch подменён — сбой «не идёт»), о переходе в группу — один раз, админу 🚨 нет
    assert state["main"] == 2 and state["backup"] == ["gpt://b1gfolder/deepseek-v4.1-flash"] * 2
    assert state["said"] == 1 and state["alarm"] == []


def test_during_outage_straight_to_backup(monkeypatch):
    llm = _backup_llm(monkeypatch)
    state = {"main": 0, "backup": [], "watch": 0, "said": 0, "alarm": []}
    _patch_backup(monkeypatch, state)

    async def run():
        L._watch = asyncio.create_task(asyncio.sleep(10))   # сбой идёт: watch_up пингует DeepSeek
        try:
            return await llm.chat([{"role": "user", "content": "привет"}])
        finally:
            L._watch.cancel()
            await llm.close()

    assert asyncio.run(run()) == "из резерва" and state["main"] == 0 and len(state["backup"]) == 1


def test_backup_fails_alarm_with_both(monkeypatch):
    llm = _backup_llm(monkeypatch)
    state = {"main": 0, "backup": [], "watch": 0, "said": 0, "alarm": []}
    _patch_backup(monkeypatch, state, backup_fails=True)

    async def run():
        try:
            await llm.chat([{"role": "user", "content": "привет"}])
        except httpx.HTTPStatusError as e:
            return e.response.status_code
        finally:
            await llm.close()

    assert asyncio.run(run()) == 401 and state["said"] == 0 and len(state["alarm"]) == 1 and state["alarm"][0]


def test_no_backup_without_key(monkeypatch):
    monkeypatch.delenv("LLM_BACKUP_KEY", raising=False)
    llm = L.LLM(base_url="https://api.test", api_key="k", backup=True)
    assert not llm._backup
    asyncio.run(llm.close())


def test_describe_backup():
    assert L.describe(_status(402), L.BACKUP_NAME) == "закончился баланс Yandex Cloud"
    assert L.describe(_status(503)) == "DeepSeek отвечает ошибкой 503"


def _openai(monkeypatch, model="gpt-5-mini", extra=None):
    monkeypatch.setattr(L, "YANDEX", False)
    monkeypatch.setattr(L, "BACKUP_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(L, "BACKUP_MODEL", model)
    monkeypatch.setattr(L, "BACKUP_EXTRA", extra if extra is not None else {"reasoning_effort": "minimal"})
    monkeypatch.setattr(L, "REASONING", "reasoning_effort" in L.BACKUP_EXTRA)


def test_backup_body_yandex():
    """Как было 03.11: модель в каталоге, temperature и max_tokens, рассуждения выключены."""
    b = L.backup_body([{"role": "user", "content": "x"}], True, 0.3, 900)
    assert b["model"].startswith("gpt://") and b["temperature"] == 0.3 and b["max_tokens"] == 900
    assert b["reasoning_effort"] == "none" and b["response_format"] == {"type": "json_object"}


def test_backup_body_openai(monkeypatch):
    """gpt-5-mini: без temperature (иначе 400), лимит — max_completion_tokens с запасом на рассуждения."""
    _openai(monkeypatch)
    b = L.backup_body([{"role": "user", "content": "x"}], True, 0.3, 900)
    assert b["model"] == "gpt-5-mini" and "temperature" not in b and "max_tokens" not in b
    assert b["max_completion_tokens"] == 2900 and b["reasoning_effort"] == "minimal"
    assert b["prompt_cache_retention"] == "24h" and b["prompt_cache_key"].startswith("avito-dialog")
    _openai(monkeypatch, "gpt-4.1-mini", {})
    b = L.backup_body([{"role": "user", "content": "x"}], False, 0.3, 900)
    assert {k: b[k] for k in ("model", "temperature", "max_completion_tokens")} == {
        "model": "gpt-4.1-mini", "temperature": 0.3, "max_completion_tokens": 900}
    assert "reasoning_effort" not in b and "response_format" not in b


def test_openai_backup_without_folder(monkeypatch):
    """Каталог нужен только Yandex; заголовка OpenAI-Project у OpenAI нет (там это id проекта — был бы 401)."""
    _openai(monkeypatch)
    monkeypatch.setattr(L, "BACKUP_FOLDER", "")
    monkeypatch.setenv("LLM_BACKUP_KEY", "sk-test")
    llm = L.LLM(base_url="https://api.test", api_key="k", backup=True)
    assert llm._backup and "OpenAI-Project" not in llm._backup.headers
    asyncio.run(llm.close())
    monkeypatch.setattr(L, "YANDEX", True)
    llm = L.LLM(base_url="https://api.test", api_key="k", backup=True)
    assert not llm._backup   # Yandex без каталога — резерва нет, как раньше
    asyncio.run(llm.close())
