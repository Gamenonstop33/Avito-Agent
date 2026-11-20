"""Реквизиты карт из VK (18.11, Макс: «карты отлетают на раз-два» — менять банк, номер и получателя самому):
список «банк | номер | получатель» → разбор → по кругу со своими реквизитами; не задано — из .env."""
from shared import sales


def test_parse_banks_formats():
    rows, errors = sales.parse_banks("Альфа-Банк | +7 900 111-22-33 | Иван И.\n"
                                     "Т-Банк; 89001112233; Пётр П.\n"
                                     "ОТП Банк — 8 (900) 444 55 66 — Анна А.\n\n")
    assert errors == []
    assert rows == [("Альфа-Банк", "+79001112233", "Иван И."), ("Т-Банк", "+79001112233", "Пётр П."),
                    ("ОТП Банк", "+79004445566", "Анна А.")]


def test_parse_banks_errors():
    rows, errors = sales.parse_banks("Сбер | нет номера | Иван\n| +79001112233 | Иван\nАльфа | +79001112233 |\n"
                                     "Т-Банк | +79001112233 | А\nт-банк | +79002223344 | Б")
    assert [r[0] for r in rows] == ["Т-Банк"] and len(errors) == 4


def test_requisites_per_bank_and_env_fallback(monkeypatch):
    monkeypatch.setattr(sales, "env", lambda k, d=None: {"PAY_PHONE": "+70000000000", "PAY_RECIPIENT": "Общий"}.get(k, d))
    monkeypatch.setattr(sales, "BANK_REQ", {"Т-Банк": ("+79001112233", "Пётр П."), "ОТП Банк": ("", "")})
    assert "Номер: +79001112233\nПолучатель: Пётр П." in sales.requisites("Т-Банк", 990)
    assert "Номер: +70000000000\nПолучатель: Общий" in sales.requisites("ОТП Банк", 990)   # не задано — .env
    card = {"method": "card", "pay_url": None, "amount": 1090, "discount": 0, "bank": "Т-Банк"}
    assert "+79001112233" in sales.pay_text(card)


def test_bank_lines_and_describe(monkeypatch):
    monkeypatch.setattr(sales, "BANK_REQ", {"Альфа-Банк": ("+79001112233", "Иван И.")})
    monkeypatch.setattr(sales, "env", lambda k, d=None: d)
    rows = [{"name": "Альфа-Банк"}, {"name": "Газпромбанк"}]
    assert sales.bank_lines(rows) == "Альфа-Банк | +79001112233 | Иван И.\nГазпромбанк | — | —"
    text = sales.describe_banks([("альфа-банк", "+79001112233", "Иван И.")], rows)
    assert "1. альфа-банк" in text and "Выключатся (бот перестанет их давать): Газпромбанк" in text
    # список из сообщения бота разбирается обратно как есть (скопировал — прислал)
    assert sales.parse_banks("Альфа-Банк | +79001112233 | Иван И.")[0] == [("Альфа-Банк", "+79001112233", "Иван И.")]
