from datetime import datetime, timezone

from services.dialog import brain
from shared import sales

BANKS = ["Т-Банк", "Альфа-Банк", "Ozon Банк"]


def test_next_bank_cycles():
    assert sales.next_bank(BANKS, None) == "Т-Банк"
    assert sales.next_bank(BANKS, "Т-Банк") == "Альфа-Банк"
    assert sales.next_bank(BANKS, "Ozon Банк") == "Т-Банк"
    assert sales.next_bank(BANKS, "Сбер") == "Т-Банк"   # банк отключили — начинаем сначала


def test_pay_method():
    # микс: до 6 мес — ИП, от 8 мес — карты, пока не пройдены круги по банкам за сутки (лимит 16)
    assert sales.pay_method(6, 0, 16, "live") == "ip"
    assert sales.pay_method(1, 0, 16, "live") == "ip"
    assert sales.pay_method(8, 15, 16, "live") == "card"
    assert sales.pay_method(12, 16, 16, "live") == "ip"
    assert sales.pay_method(20, 0, 16, "only") == "ip"      # только ИП
    assert sales.pay_method(20, 0, 16, "test") == "ip"      # тестовый клиент — всегда ИП
    assert sales.pay_method(3, 99, 16, None) == "card"      # только карты


def test_ip_mode(monkeypatch):
    monkeypatch.setattr(sales, "ALFA_READY", True)
    monkeypatch.setattr(sales, "ALFA_TEST_CHATS", {"u2i-test"})
    assert sales.ip_mode(1, "u2i-test", "card") == "test"      # тестовый чат — всегда ИП
    assert sales.ip_mode(1, "u2i-other", "card") is None       # только карты
    assert sales.ip_mode(1, "u2i-other", "mix") == "live"
    assert sales.ip_mode(1, "u2i-other", "ip") == "only"
    monkeypatch.setattr(sales, "ALFA_READY", False)            # нет логина Альфы — только карты
    assert sales.ip_mode(1, "u2i-test", "ip") is None
    assert "✅ 🔀 Микс" in sales.pay_mode_text("mix") and "Логин и пароль Альфа-Банка не заданы" in sales.pay_mode_text("mix")


def test_pay_text():
    ip = {"method": "ip", "pay_url": "https://qr.nspk.ru/X", "amount": 990, "discount": 100, "bank": sales.IP_BANK}
    assert "https://qr.nspk.ru/X" in sales.pay_text(ip) and "990 ₽ (со скидкой" in sales.pay_text(ip)
    assert "Номер:" not in sales.pay_text(ip)
    card = {"method": "card", "pay_url": None, "amount": 1090, "discount": 0, "bank": "Т-Банк"}
    assert "Банк: Т-Банк" in sales.pay_text(card)


def test_valid_amount():
    assert sales.valid_amount(1090, 1090, new=False)
    assert sales.valid_amount(1090, 990, new=True)
    assert not sales.valid_amount(1090, 990, new=False)
    assert not sales.valid_amount(1090, 980, new=True)
    assert not sales.valid_amount(None, 1000, new=True)   # такого срока нет в прайсе


def test_add_months():
    assert sales.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1).day == 28
    assert sales.add_months(datetime(2026, 11, 15, tzinfo=timezone.utc), 3) == datetime(2027, 2, 15, tzinfo=timezone.utc)


def test_issue_text():
    assert "код" in sales.issue_text("console", {"email": "a@b.c", "login": "a@b.c", "password": "p"})
    pc = sales.issue_text("pc", {"email": "a@b.c", "login": "a@b.c", "password": "p"})
    assert "Почта: a@b.c" in pc and "Пароль: p" in pc and "Логин" not in pc
    assert "подготовлю" in sales.issue_text("pc", None)
    assert not sales.AUTO_ISSUE   # склад тестовый — выдаёт владелец


def test_parse_order():
    d = brain.parse_decision('{"reply": "Вот реквизиты:", "handoff": null, "stage": "оплата", '
                             '"order": {"product": "console", "months": "12", "price": 4290}}')
    assert d.order == {"product": "console", "months": 12, "price": 4290}
    assert brain.parse_decision('{"reply": "", "order": {"product": "ps5", "months": 1, "price": 1}}').order is None
    assert brain.parse_decision('{"reply": "", "order": {"months": 1}}').order is None


def test_personal_order():
    assert sales.issue_text("personal", None) == sales.PERSONAL_PAID
    d = brain.parse_decision('{"reply": "Вот реквизиты:", "handoff": null, "stage": "оплата", '
                             '"order": {"product": "personal", "months": 4, "price": 4190}}')
    assert d.order == {"product": "personal", "months": 4, "price": 4190}
    assert "personal" in sales.NO_DISCOUNT   # скидки на личный аккаунт нет (worker передаёт new=False)


def test_parse_accounts():
    today = datetime(2026, 10, 7, tzinfo=timezone.utc)
    rows, errors = sales.parse_accounts("a@outlook.com Pass1 r@mail.ru 6\n"
                                        "b@outlook.com; Pass2; 4 мес; 01.10\n"
                                        "c@outlook.com Pass3 r3@mail.ru 2 пк\n"
                                        "мусор без почты\n"
                                        "d@outlook.com Pass4 r4@mail.ru 99", today)
    assert [(r["email"], r["product"], r["months"], r["recovery_email"]) for r in rows] == [
        ("a@outlook.com", "console", 6, "r@mail.ru"), ("b@outlook.com", "console", 4, None),
        ("c@outlook.com", "pc", 2, "r3@mail.ru")]
    assert rows[0]["expires_at"] == datetime(2027, 4, 7, tzinfo=timezone.utc)
    assert rows[1]["activated_at"].date().isoformat() == "2026-10-01"
    assert len(errors) == 2   # строка без почты и срок 99


def test_instruction_by_slot():
    assert "сделать домашней" in sales.instruction(1) and "сделать домашней" in sales.instruction(None)
    assert "не изменять" in sales.instruction(2) and sales.SLOTS["console"] == 2


def test_pc_rental_text_and_paid_status():
    pc = sales.issue_text("pc", {"email": "a@b.c", "login": "a@b.c", "password": "p", "recovery_email": "r@b.c"})
    assert "Резервная почта: r@b.c" in pc and "Microsoft Store" in pc and "Использовать пароль" in pc
    st = brain.order_status({"status": "paid", "product": "console", "months": 6, "amount": 2290, "stock_id": 5})
    assert "ЗАКАЗ ОПЛАЧЕН" in st and "handoff code" in st
    assert "сам" in brain.order_status({"status": "paid", "product": "console", "months": 6, "amount": 2290,
                                        "stock_id": None})
    assert brain.parse_decision('{"reply": "Входим", "handoff": {"reason": "code"}}').handoff["reason"] == "code"


def test_new_client_invoice_with_discount():
    from shared.sales import order_amount, requisites
    assert order_amount(1090, "console", new=True) == 990          # новому — скидка сразу, не по просьбе
    assert order_amount(1090, "pc", new=True) == 990
    assert order_amount(3000, "personal", new=True) == 3000       # личный аккаунт — без скидки
    assert order_amount(1090, "console", new=False) == 1090
    assert "990 ₽ (со скидкой за отзыв)" in requisites("Альфа", 990, True)


def test_review_ask_after_connection():
    from shared.sales import instruction, issue_text
    assert instruction(1, True).endswith("Скидку мы сделали как раз за него. "
                                          "Отзыв можно оставить в нашем профиле → «Отзывы» → «Оставить отзыв».")
    assert "100" not in instruction(2, True).split("\n")[-1]       # размер скидки не называем
    assert "Скидку" not in instruction(2, False)
    pc = issue_text("pc", {"email": "a@b.c", "login": None, "password": "p"}, True)
    assert pc.endswith("«Оставить отзыв».") and "Скидку мы сделали" in pc
