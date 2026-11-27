"""Пробная оплата через Platega: транзакция СБП QR → ссылка (НСПК, если включён H2H) → ждём оплату.

Запуск:  python scripts/platega_pay_test.py [сумма, ₽ — по умолчанию 10]
Ключи — PLATEGA_MERCHANT_ID / PLATEGA_SECRET из .env, не выводятся.
Возврат — из личного кабинета Platega.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import platega  # noqa: E402


async def pay(rub: int) -> None:
    c = platega.PlategaClient()
    try:
        t = await c.create(f"test-{int(time.time())}", rub, f"Тестовая оплата {rub} руб.", None, "test")
        print(f"Транзакция: {t['id']} на {rub} ₽, живёт {t['ttl']}")
        kind = "НСПК ✅ (H2H работает)" if "qr.nspk.ru" in t["url"] else "страница Platega ⚠️ (H2H не включён)"
        print(f"Ссылка — {kind}:\n{t['url']}")
        wait = int(t["ttl"].total_seconds()) + 60
        print(f"Жду оплату (до {wait // 60} мин, Ctrl+C — выход)…")
        last = None
        for _ in range(wait // 5):
            st = (await c.status(t["id"])).get("status")
            if st != last:
                print(f"  статус {st}")
                last = st
            if st == platega.CONFIRMED:
                print("Оплачено ✅")
                return
            if st == platega.CANCELED:
                print("Отменена / истекла ❌")
                return
            await asyncio.sleep(5)
        print("Не дождался оплаты")
    except platega.PlategaError as e:
        print(f"Ошибка Platega: {e}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(pay(int(sys.argv[1]) if len(sys.argv) > 1 else 10))
