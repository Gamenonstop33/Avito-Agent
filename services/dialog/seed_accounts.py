"""Тестовый склад аккаунтов: по N фиктивных аккаунтов на каждый тип и срок из прайса (пока нет настоящей таблицы).

Запуск: python -m services.dialog.seed_accounts [N]   (повторный запуск добивает до N свободных)
"""
import asyncio
import sys

from shared import catalog, sales
from shared.db import connect


async def main(n: int) -> None:
    db = await connect(catalog.SCHEMA + sales.SCHEMA)
    _, prices = await catalog.load(db)
    added = 0
    for product, rows in prices.items():
        for months in rows:
            free = await db.fetchval("SELECT count(*) FROM sales.accounts WHERE product=$1 AND months=$2 "
                                     "AND status='free'", product, months)
            for i in range(free, n):
                tag = f"{product}{months}m{i + 1}"
                await db.execute("INSERT INTO sales.accounts (product, months, email, login, password, note) "
                                 "VALUES ($1,$2,$3,$3,$4,'test')", product, months, f"test.{tag}@example.com",
                                 f"Test-{tag}!")
                added += 1
    print(f"добавлено тестовых аккаунтов: {added}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
