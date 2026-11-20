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


def test_second_account_schemas(monkeypatch):
    """Копия бота второго аккаунта (DB_NS=2, 28.10): свои схемы, общий только склад учёток."""
    monkeypatch.setattr(db, "NS", "2")
    db.ns_sql.cache_clear()
    try:
        assert db.ns_sql("SELECT * FROM gateway.chats c JOIN crm.customers u ON true") == \
            "SELECT * FROM gateway2.chats c JOIN crm2.customers u ON true"
        assert db.ns_sql("CREATE SCHEMA IF NOT EXISTS sales; CREATE SCHEMA owner") == \
            "CREATE SCHEMA IF NOT EXISTS sales2; CREATE SCHEMA owner2"
        assert db.ns_sql("SELECT sales.orders.stock_id FROM sales.orders o JOIN sales.accounts a ON a.id=o.stock_id "
                         "WHERE a.term = '2'::sales.term AND sales.slot_day(a.note) IS NULL") == \
            ("SELECT sales2.orders.stock_id FROM sales2.orders o JOIN sales.accounts a ON a.id=o.stock_id "
             "WHERE a.term = '2'::sales.term AND sales.slot_day(a.note) IS NULL")
        # выдача со склада — одна блокировка на оба аккаунта, круг банков — у каждого свой
        assert db.ns_sql("SELECT pg_advisory_xact_lock(hashtext('sales.stock'))") == \
            "SELECT pg_advisory_xact_lock(hashtext('sales.stock'))"
        assert db.ns_sql("SELECT pg_advisory_xact_lock(hashtext('sales.bank'))") == \
            "SELECT pg_advisory_xact_lock(hashtext('sales2.bank'))"
        assert db.ns_sql("SELECT to_regclass('crm.purchases'), x.presales.y") == \
            "SELECT to_regclass('crm2.purchases'), x.presales.y"
        # правила владельца — общие (30.10), а свои ночные прогоны и чаты — у каждой копии
        assert db.ns_sql("SELECT id FROM dialog.rules WHERE chat_id IN (SELECT id FROM gateway.chats); "
                         "SELECT max(ran_at) FROM dialog.lesson_runs") == \
            ("SELECT id FROM dialog.rules WHERE chat_id IN (SELECT id FROM gateway2.chats); "
             "SELECT max(ran_at) FROM dialog2.lesson_runs")
    finally:
        db.ns_sql.cache_clear()
    monkeypatch.setattr(db, "NS", "")
    assert db.ns_sql("SELECT * FROM gateway.chats") == "SELECT * FROM gateway.chats"
    db.ns_sql.cache_clear()
