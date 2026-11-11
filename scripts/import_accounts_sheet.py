"""Перенос Google-таблицы учёток владельца в sales.accounts (15.10; один раз, повтор — с --replace).
Выгрузка: в Google Таблицах «Файл → Скачать → Microsoft Excel (.xlsx)», файл — в data/ на сервере, после переноса удалить:
  docker compose -f docker-compose.prod.yml run --rm -v "$PWD/scripts:/app/scripts:ro" owner-bot \\
      python scripts/import_accounts_sheet.py data/accounts.xlsx [--dry] [--replace]
Адрес таблицы в репозиторий не кладём: он публичный, а в таблице пароли. Разбор — shared/sheet_import.py."""
import argparse
import asyncio
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import sales  # noqa: E402
from shared.db import connect  # noqa: E402
from shared.sheet_import import parse_sheet, read_xlsx  # noqa: E402

COLS = ("sheet_row", "email", "password", "recovery_email", "months", "activated_at", "written_off", "note",
        *(f"slot{k}_{f}" for k in (1, 2, 3) for f in ("client", "from", "months")))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help=".xlsx — выгрузка таблицы владельца")
    ap.add_argument("--dry", action="store_true", help="только разобрать и показать итог")
    ap.add_argument("--replace", action="store_true", help="сначала убрать прошлый перенос (кроме выданных ботом)")
    args = ap.parse_args()
    today = datetime.now(sales.TZ).date()
    recs = parse_sheet(read_xlsx(args.path)[1:], today)   # первая строка — заголовок
    active = [r for r in recs if not r["written_off"]]
    print(f"строк {len(recs)}: действующих {len(active)}, списанных {len(recs) - len(active)}; "
          f"пустая основа — {sum(not r['slot1_client'] for r in active)}, пустой доп — "
          f"{sum(not r['slot2_client'] for r in active)}; сверить (в заметке «было:» или срок не разобран) — "
          f"{sum(bool(r['note']) and ('было:' in r['note'] or 'не разобран' in r['note']) for r in recs)}")
    if args.dry:
        return
    for r in recs:   # дата активации — полночь по Москве; без даты — сегодня (триггер)
        r["activated_at"] = r["activated_at"] and datetime.combine(r["activated_at"], time(), sales.TZ)
    db = await connect(sales.SCHEMA)
    async with db.acquire() as con, con.transaction():
        if await con.fetchval("SELECT count(*) FROM sales.accounts WHERE sheet_row IS NOT NULL") and not args.replace:
            raise SystemExit("перенос уже был — повторить с --replace")
        if args.replace:
            print(await con.execute("DELETE FROM sales.accounts WHERE sheet_row IS NOT NULL AND id NOT IN "
                                    "(SELECT stock_id FROM sales.orders WHERE stock_id IS NOT NULL)"))
        values = ", ".join(f"${i + 1}" for i in range(len(COLS)))
        await con.executemany(f"INSERT INTO sales.accounts ({', '.join(COLS)}) VALUES ({values})",
                              [tuple(r[c] for c in COLS) for r in recs])
        print(dict(await con.fetchrow(
            "SELECT count(*) AS всего, count(*) FILTER (WHERE status <> 'off') AS действующих, "
            "count(*) FILTER (WHERE status = 'free') AS со_свободным_местом FROM sales.accounts "
            "WHERE sheet_row IS NOT NULL")))
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
