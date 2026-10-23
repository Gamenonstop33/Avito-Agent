"""Схемы БД при одновременном старте сервисов — по очереди (15.10, 20.10: deadlock на sales.SCHEMA)."""
import asyncio
from contextlib import asynccontextmanager

from shared import db


class FakeCon:
    """Блокировка Postgres: pg_advisory_xact_lock держится до конца транзакции."""
    def __init__(self, lock, log):
        self.lock, self.log, self.held = lock, log, False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        finally:
            if self.held:
                self.held = False
                self.lock.release()

    async def execute(self, sql):
        if "pg_advisory_xact_lock" in sql:
            await self.lock.acquire()
            self.held = True
            return
        assert self.held, "схема выполняется без блокировки"
        self.log.append(f"{sql}:start")
        await asyncio.sleep(0.01)
        self.log.append(f"{sql}:end")


def test_schema_applied_one_at_a_time(monkeypatch):
    lock, log = asyncio.Lock(), []

    class FakePool:
        @asynccontextmanager
        async def acquire(self):
            yield FakeCon(lock, log)

    async def create_pool(*_, **__):
        return FakePool()

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)

    async def scenario():
        await asyncio.gather(db.connect("a"), db.connect("b"))

    asyncio.run(scenario())
    assert log in (["a:start", "a:end", "b:start", "b:end"], ["b:start", "b:end", "a:start", "a:end"])
