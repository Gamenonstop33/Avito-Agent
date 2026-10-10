import asyncpg

from shared.config import env


async def connect(schema_sql: str | None = None) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(env("DATABASE_URL"), min_size=1, max_size=5)
    if schema_sql:
        async with pool.acquire() as con:
            await con.execute(schema_sql)
    return pool


async def single_instance(pool: asyncpg.Pool, name: str) -> asyncpg.Connection:
    """Не даёт запустить вторую копию сервиса (две копии = двойные ответы). Держим соединение с блокировкой."""
    con = await pool.acquire()
    if not await con.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", name):
        await pool.release(con)
        raise SystemExit(f"{name} уже запущен — вторая копия не нужна (двойные ответы). Закройте лишнюю.")
    return con
