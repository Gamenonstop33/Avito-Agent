import asyncpg

from shared.config import env


async def connect(schema_sql: str | None = None, max_size: int = 5) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(env("DATABASE_URL"), min_size=1, max_size=max_size)
    if schema_sql:
        # сервисы стартуют разом и гоняют одни и те же ALTER/UPDATE (sales.SCHEMA) — по очереди, иначе deadlock
        # (15.10, 20.10). Ключ из двух int4 — не пересекается с блокировками single_instance (один ключ)
        async with pool.acquire() as con, con.transaction():
            await con.execute("SELECT pg_advisory_xact_lock(hashtext('db_schema'), 0)")
            await con.execute(schema_sql)
    return pool


async def single_instance(pool: asyncpg.Pool, name: str) -> asyncpg.Connection:
    """Не даёт запустить вторую копию сервиса (две копии = двойные ответы). Держим соединение с блокировкой."""
    con = await pool.acquire()
    if not await con.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", name):
        await pool.release(con)
        raise SystemExit(f"{name} уже запущен — вторая копия не нужна (двойные ответы). Закройте лишнюю.")
    return con
