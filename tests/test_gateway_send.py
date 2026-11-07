"""Отправка ответов (18.10): уже отменённый ответ не уходит; клиенту ответил Макс, пока бот выжидал паузу, — ответ
бота не отправляется."""
import asyncio
from datetime import datetime, timedelta, timezone

from services.avito_gateway.worker import Gateway

T0 = datetime(2026, 10, 16, 11, 48, 22, tzinfo=timezone.utc)   # сообщение клиента


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class FakeCon(Tx):
    def __init__(self, row, ours=()):
        self.row, self.ours, self.sql = row, set(ours), []

    def transaction(self):
        return Tx()

    async def fetchrow(self, sql, *_):
        return self.row   # None — ответ отменили, пока gateway шёл по списку

    async def fetch(self, sql, ids):
        return [{"message_id": i} for i in ids if i in self.ours]

    async def execute(self, sql, *args):
        self.sql.append(sql)


class FakePool:
    def __init__(self, con):
        self.con = con

    async def fetch(self, *_):
        return [{"id": 1}]

    async def fetchrow(self, *_):
        return {"all_chats": True, "whitelist": [], "started_at": T0}

    def acquire(self):
        return self.con


class FakeAvito:
    def __init__(self, msgs):
        self.msgs, self.sent = msgs, []

    async def messages(self, *_, **__):
        return self.msgs

    async def send_text(self, me, chat_id, text):
        self.sent.append(text)
        return {"id": "new"}


def row(**kw):
    return {"id": 1, "chat_id": "c", "text": "Да, по этому же номеру.", "broadcast": False, "attempts": 0,
            "created_at": T0 + timedelta(seconds=7), "reply_to": T0} | kw


def msg(mid, seconds, direction="out", text="x"):
    return {"id": mid, "direction": direction, "type": "text", "created": (T0 + timedelta(seconds=seconds)).timestamp(),
            "content": {"text": text}}


def run(con, av):
    asyncio.run(Gateway(FakePool(con), av, 1).send_due())


def test_owner_wrote_after_client_cancels_reply():
    con, av = FakeCon(row()), FakeAvito([msg("m1", 10), msg("c1", 0, "in")])   # Макс через 10 с после клиента
    run(con, av)
    assert not av.sent and any("Макс пишет сам" in s for s in con.sql)


def test_old_owner_message_and_own_messages_do_not_block():
    old = [msg("m0", -600), msg("b1", 2), msg("c1", 0, "in")]   # Макс писал до клиента, b1 — прошлый ответ бота
    con, av = FakeCon(row(), ours={"b1"}), FakeAvito(old)
    run(con, av)
    assert av.sent == ["Да, по этому же номеру."]


def test_retry_text_is_not_owner():
    """Прошлая попытка дошла до Avito (таймаут) — это не Макс: ищем её и не шлём второй раз."""
    con, av = FakeCon(row(attempts=1)), FakeAvito([msg("p1", 5, text="Да, по этому же номеру.")])
    run(con, av)
    assert not av.sent and not any("Макс пишет сам" in s for s in con.sql)


def test_cancelled_meanwhile_not_sent():
    con, av = FakeCon(None), FakeAvito([])
    run(con, av)
    assert not av.sent and not con.sql
