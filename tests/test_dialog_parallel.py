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
        rows = [{"chat_id": f"c{i}", "account_id": 1, "last": old} for i in range(6)]
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
        assert started.count("c0") == 1 and set(started) == {f"c{i}" for i in range(6)}
        release.set()
        await asyncio.gather(*d.tasks)
        assert not d.busy

    asyncio.run(scenario())
