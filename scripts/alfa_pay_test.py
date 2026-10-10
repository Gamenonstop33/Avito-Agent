"""Пробная оплата через Альфа-Банк: заказ → ссылки на оплату → ждём оплату. Возврат — отдельной командой.

Запуск:  python scripts/alfa_pay_test.py [сумма, ₽ — по умолчанию 10]
Возврат: python scripts/alfa_pay_test.py --refund <orderId> <сумма>
Логин/пароль — ALFA_USER / ALFA_PASSWORD из .env, не выводятся.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import alfa  # noqa: E402

WAIT = 15 * 60   # сколько ждём оплату, с


async def pay(rub: int) -> None:
    c = alfa.AlfaClient()
    try:
        order_id, form_url = await c.register(f"test-{int(time.time())}", rub, f"Тестовая оплата {rub} руб.", ttl=WAIT)
        print(f"Заказ: {order_id} на {rub} ₽")
        print(f"Платёжная страница: {form_url}")
        try:
            print(f"Ссылка СБП: {await c.sbp_link(order_id)}")
        except alfa.AlfaError as e:
            print(f"Ссылка СБП не получена ({e}) — платите со страницы")
        print("Жду оплату (до 15 мин, Ctrl+C — выход)…")
        last = None
        for _ in range(WAIT // 5):
            s = await c.status(order_id)
            st = s.get("orderStatus")
            if st != last:
                print(f"  статус {st} {s.get('actionCodeDescription', '')}")
                last = st
            if st == alfa.PAID:
                print(f"Оплачено ✅ Возврат: python scripts/alfa_pay_test.py --refund {order_id} {rub}")
                return
            if st in (alfa.DECLINED, alfa.REVERSED):
                print("Оплата отклонена/отменена ❌")
                return
            await asyncio.sleep(5)
        print("Не дождались оплаты")
    finally:
        await c.close()


async def refund(order_id: str, rub: int) -> None:
    c = alfa.AlfaClient()
    try:
        await c.refund(order_id, rub)
        s = await c.status(order_id)
        print(f"Возврат отправлен, статус заказа: {s.get('orderStatus')} (4 — возврат)")
    finally:
        await c.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if a[:1] == ["--refund"]:
            asyncio.run(refund(a[1], int(a[2])))
        else:
            asyncio.run(pay(int(a[0]) if a else 10))
    except alfa.AlfaError as e:
        sys.exit(f"Ошибка шлюза: {e}")
