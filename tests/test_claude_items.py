"""20.11: объявления про Claude — бот не ведёт, каждое сообщение клиента только админу в VK."""
import asyncio
import json
import re
from datetime import datetime, timezone

from shared.kb_classes import CLAUDE_RE, claude_item, classify_dialog, outside_item


def test_claude_item():
    assert claude_item("Готовый Claude AI с верификацией")
    assert claude_item("Верификация Claude KYC—(подтверждение личности)")
    assert claude_item("Аккаунт Клод с подпиской")
    assert not claude_item("Подписка Xbox Game Pass Ultimate 1 - 12 месяцев") and not claude_item(None)
    assert outside_item("Готовый Claude AI") and outside_item("Смена Региона Стим")
    assert not outside_item("Xbox game pass ultimate подписка")
    assert classify_dialog("Готовый Claude AI с верификацией", []) == "claude"   # не в базе знаний
    assert re.search("(?i)" + CLAUDE_RE, "CLAUDE")


def test_not_ignored_sql_skips_claude():
    from services.dialog import worker
    assert CLAUDE_RE in worker.NOT_IGNORED and "gateway.ignored" in worker.NOT_IGNORED


class _DB:
    def __init__(self, title):
        self.title, self.calls = title, []

    async def fetchrow(self, q, *a):
        self.calls.append(q)
        if "dialog.chats" in q:
            return {"state": "bot", "handoff_reason": None, "handoff_at": None}
        return {"item_title": self.title, "client_id": 374994233, "client_name": "Иван"}

    async def fetch(self, q, *a):
        raise AssertionError(f"бот не должен идти дальше: {q[:60]}")

    async def execute(self, q, *a):
        raise AssertionError(f"бот не должен ничего менять: {q[:60]}")


def _ev(kind, text):
    return {"id": 1, "kind": kind, "message_id": "m1", "created_at": datetime.now(timezone.utc),
            "payload": json.dumps({"text": text, "type": "text"})}


def test_claude_chat_only_alerts_admin(monkeypatch):
    from services.dialog import worker
    sent = []

    async def admins(text):
        sent.append(text)

    monkeypatch.setattr(worker.alerts, "notify_admins", admins)
    d = object.__new__(worker.Dialog)
    d.db = _DB("Готовый Claude AI с верификацией")
    asyncio.run(d.handle("u2i-claude", 319641330, [_ev("client_message", "Он уже с подпиской?")]))
    assert len(sent) == 1 and "CLAUDE" in sent[0] and "Он уже с подпиской?" in sent[0] and "Иван" in sent[0]
    assert "u2i-claude" in sent[0]

    sent.clear()   # пишет владелец — ни уведомления, ни смены состояния чата
    asyncio.run(d.handle("u2i-claude", 319641330, [_ev("owner_message", "Да он готовый")]))
    assert not sent
