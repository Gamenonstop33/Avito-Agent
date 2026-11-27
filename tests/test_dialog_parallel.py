"""Параллельная обработка чатов (15.10: один ответ думал 22 мин — стояли все чаты)."""
import asyncio
from datetime import datetime, timedelta, timezone

from services.dialog import worker


class FakeDB:
    def __init__(self, chats):
        self.chats = chats

    async def fetch(self, *_):
        return self.chats


def test_tick_parallel_and_one_task_per_chat():
    async def scenario():
        old = datetime.now(timezone.utc) - timedelta(seconds=30)
        n = worker.PARALLEL + 2   # чатов больше, чем обрабатывается разом
        rows = [{"chat_id": f"c{i}", "account_id": 1, "last": old} for i in range(n)]
        d = worker.Dialog.__new__(worker.Dialog)
        d.db, d.busy, d.tasks = FakeDB(rows), set(), set()
        started, release = [], asyncio.Event()

        async def process_chat(chat_id, account_id):
            started.append(chat_id)
            d.db.chats = [r for r in d.db.chats if r["chat_id"] != chat_id]   # события взяты в работу
            if chat_id == "c0":
                await release.wait()   # «долгий» чат

        d.process_chat = process_chat
        await d.tick()
        await asyncio.sleep(0)
        assert len(started) == worker.PARALLEL           # не больше PARALLEL сразу
        await asyncio.sleep(0.01)
        await d.tick()                                    # быстрые закончились — берём следующие, c0 ещё занят
        await asyncio.sleep(0.01)
        assert started.count("c0") == 1 and set(started) == {f"c{i}" for i in range(n)}
        release.set()
        await asyncio.gather(*d.tasks)
        assert not d.busy

    asyncio.run(scenario())


def test_dark_owner_awake():
    """Ночь бота — только если Макс не на связи (16.10: в 07:47 он уже работал, а бот писал «проверим утром»)."""
    class DB:
        awake = False

        async def fetchval(self, *_):
            return self.awake

    d = worker.Dialog.__new__(worker.Dialog)
    d.db = DB()
    msk = timezone(timedelta(hours=3))
    night, day = datetime(2026, 10, 16, 5, 47, tzinfo=msk), datetime(2026, 10, 16, 12, tzinfo=msk)
    assert asyncio.run(d.dark(night)) and not asyncio.run(d.dark(day))
    d.db.awake = True
    assert not asyncio.run(d.dark(night))


def test_digest_counts_by_chat(monkeypatch):
    """Сводка за ночь — по чатам (29.10: было «чаты − случаи»): без ответа — списком, «спасибо» без ответа — норма."""
    sent = []

    async def send_alert(db, text, *_, **__):
        sent.append(text)

    monkeypatch.setattr(worker.alerts, "send_alert", send_alert)
    d = worker.Dialog.__new__(worker.Dialog)
    d.db = None

    def chat(cid, bot=False, owner=False, text="?"):
        return {"chat_id": cid, "by_bot": bot, "by_owner": owner, "client_text": text}

    chats = [chat("c1", bot=True), chat("c2", owner=True), chat("c3", text="есть GTA?"), chat("c4", text="спасибо"),
             chat("c5", bot=True)]
    cases = [{"id": 2, "chat_id": "c5", "status": "deferred", "reason": "payment", "summary": None,
              "client_text": "оплатил"},
             {"id": 1, "chat_id": "c6", "status": "answered", "reason": "game", "summary": None, "client_text": "x"}]
    asyncio.run(d.digest(chats, cases, quiet={"c4"}))
    text = sent[0]
    assert "Писали клиентов: 5. Бот ответил сам: 2. Ответили в Avito сами: 1. Передано человеку: 2." in text
    assert "Ждут ответа (1)" in text and "Уже решено ночью: 1" in text
    assert "Остались без ответа (1)" in text and "есть GTA?" in text and "спасибо" not in text
    assert "закрыты" not in text
