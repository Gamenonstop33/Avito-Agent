import re
from functools import lru_cache

import asyncpg

from shared.config import NS, env

# Копия бота второго аккаунта (DB_NS=2, 28.10) живёт в той же базе в своих схемах gateway2, crm2, catalog2… — запросы
# переписываются на лету, код сервисов тот же. Общий только склад учёток (таблица владельца в NocoDB) с его типами,
# функциями и блокировкой выдачи: место на учётке не выдаётся дважды и из двух аккаунтов. С 30.10 общие и правила
# владельца (dialog.rules): одна база знаний на оба аккаунта, правило из любого сообщества VK действует в обоих.
SCHEMAS = ("gateway", "dialog", "crm", "catalog", "owner", "broadcast", "sales", "content")
SHARED = {"sales.accounts", "sales.accounts_id_seq", "sales.accounts_sheet", "sales.platform", "sales.term",
          "sales.slot_day", "sales.slot_months", "sales.stock", "dialog.rules", "dialog.rules_id_seq",
          "dialog.kb_edits", "dialog.kb_edits_id_seq"}
_NAME = re.compile(r"\b(%s)\.(\w+)" % "|".join(SCHEMAS))
_SCHEMA = re.compile(r"\b(SCHEMA(?: IF NOT EXISTS)?) (%s)\b" % "|".join(SCHEMAS))


@lru_cache(maxsize=4096)
def ns_sql(sql: str) -> str:
    if not NS:
        return sql
    sql = _NAME.sub(lambda m: m[0] if m[0] in SHARED else f"{m[1]}{NS}.{m[2]}", sql)
    return _SCHEMA.sub(lambda m: f"{m[1]} {m[2]}{NS}", sql)


class Connection(asyncpg.Connection):
    """Соединение копии второго аккаунта: каждый запрос — в её схемах (ns_sql)."""

    async def execute(self, query, *args, **kw):
        return await super().execute(ns_sql(query), *args, **kw)

    async def executemany(self, command, args, **kw):
        return await super().executemany(ns_sql(command), args, **kw)

    async def fetch(self, query, *args, **kw):
        return await super().fetch(ns_sql(query), *args, **kw)

    async def fetchrow(self, query, *args, **kw):
        return await super().fetchrow(ns_sql(query), *args, **kw)

    async def fetchval(self, query, *args, **kw):
        return await super().fetchval(ns_sql(query), *args, **kw)

    async def prepare(self, query, **kw):
        return await super().prepare(ns_sql(query), **kw)

    async def copy_records_to_table(self, table_name, *, schema_name=None, **kw):
        if schema_name in SCHEMAS and f"{schema_name}.{table_name}" not in SHARED:
            schema_name += NS
        return await super().copy_records_to_table(table_name, schema_name=schema_name, **kw)


CONNECTION = Connection if NS else asyncpg.Connection


async def connect_one() -> asyncpg.Connection:
    return await asyncpg.connect(env("DATABASE_URL"), connection_class=CONNECTION)


async def connect(schema_sql: str | None = None, max_size: int = 5) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(env("DATABASE_URL"), min_size=1, max_size=max_size, connection_class=CONNECTION)
    if schema_sql:
        # сервисы стартуют разом и гоняют одни и те же ALTER/UPDATE (sales.SCHEMA) — по очереди, иначе deadlock
        # (15.10, 20.10). Ключ из двух int4 — не пересекается с блокировками single_instance (один ключ)
        async with pool.acquire() as con, con.transaction():
            await con.execute("SELECT pg_advisory_xact_lock(hashtext('db_schema'), 0)")
            await con.execute(schema_sql)
    return pool


async def single_instance(pool: asyncpg.Pool, name: str) -> asyncpg.Connection:
    """Не даёт запустить вторую копию сервиса (две копии = двойные ответы). Держим соединение с блокировкой."""
    name += NS   # копия второго аккаунта — свой сервис
    con = await pool.acquire()
    if not await con.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", name):
        await pool.release(con)
        raise SystemExit(f"{name} уже запущен — вторая копия не нужна (двойные ответы). Закройте лишнюю.")
    return con
