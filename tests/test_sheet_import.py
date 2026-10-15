from datetime import date

from shared.sales import free_slot
from shared.sheet_import import parse_account, parse_sheet, parse_users

TODAY = date(2026, 10, 15)


def test_account_cell_formats():
    a = parse_account("2m\nab12@outlook.com\nPass123\nrec@mail.ru\nrecpass")
    assert (a["term"], a["email"], a["password"], a["recovery"], a["extra"]) == \
        (2, "ab12@outlook.com", "Pass123", "rec@mail.ru", "recpass")
    a = parse_account("ab12@outlook.sg Pass123 \nrec@fringmail.com Rec!1")   # пары в одну строку
    assert (a["email"], a["password"], a["recovery"], a["extra"]) == ("ab12@outlook.sg", "Pass123", "rec@fringmail.com",
                                                                       "Rec!1")
    a = parse_account("\nab12@outlook.com:Pass123")   # «почта:пароль»
    assert (a["email"], a["password"]) == ("ab12@outlook.com", "Pass123")
    assert parse_account("2\nab12@outlook.com\nPass123")["term"] == 2


def test_users_cell():
    e = parse_users("Ольга 1.12.25 12 мес\nИван 08.12. - 14 мес вышел\n")
    assert [(x["text"], x["date"], x["months"], x["pc"]) for x in e] == [
        ("Ольга", (1, 12, 2025), 12, False), ("Иван вышел", (8, 12, None), 14, False)]
    e = parse_users("Антон 12.10. 2 мес Виктор 12.10 2 мес")   # двое в одной строке
    assert [(x["text"], x["date"][:2], x["months"]) for x in e] == [("Антон", (12, 10), 2), ("Виктор", (12, 10), 2)]
    e = parse_users("Гурам 29.01. 12 мес + ПК\nДарья 29.01. 12 мес\nИгорь 07.05. 6 мес ПК")
    assert [x["pc"] for x in e] == [False, False, True]   # «+ ПК» — консольный клиент, «ПК» — место ПК
    e = parse_users("Константин Шеин\n\n02.03. 12 мес G")   # имя строкой выше даты
    assert [(x["text"], x["months"]) for x in e] == [("Константин Шеин G", 12)]


def test_sheet_slots_years_and_write_off():
    rows = [
        (2, {"A": "a1@outlook.com\np1\nr1@mail.ru", "B": "2 мес", "C": "Ольга 28.11.25 2 мес\nИван 06.12.25 2 мес"}),
        (3, {"A": "a2@outlook.com\np2\nr2@mail.ru", "B": "10 мес", "C": "Мария 05.01. 12 мес\nАнна 05.01. 12 мес\n"
                                                                         "Игорь 07.05. 6 мес ПК"}),
        (4, {"A": "a3@outlook.com\np3", "C": "Юля 04.03. 2 мес\nПётр 04.09. 2 мес", "D": "ЗАМЕНА"}),   # опечатка
        (5, {"A": "4m\na4@outlook.com\np4", "C": "Сергей 14.10 6 мес"}),
        (6, {"A": "2m\na5@outlook.com\np5"}),
    ]
    r = {x["sheet_row"]: x for x in parse_sheet(rows, TODAY)}
    assert r[2]["activated_at"] == date(2025, 11, 28) and r[2]["written_off"]
    assert (r[3]["slot1_client"], r[3]["slot2_client"], r[3]["slot3_client"]) == ("Мария", "Анна", "Игорь")
    assert r[3]["slot3_from"] == date(2026, 5, 7) and not r[3]["written_off"]   # 10 мес с 05.01.2026
    assert "срок учётки не разобран" in r[4]["note"] and "ЗАМЕНА" in r[4]["note"]
    assert r[5]["activated_at"] == date(2026, 10, 14) and r[5]["months"] == 4 and r[5]["slot2_client"] is None
    assert r[6]["activated_at"] is None and not r[6]["written_off"] and r[6]["slot1_client"] is None


def test_duplicate_email_keeps_last_active():
    rows = [(2, {"A": "a@outlook.com\np", "B": "4 мес", "C": "Оля 01.10.26 2 мес"}),
            (3, {"A": "A@outlook.com\np", "B": "4 мес", "C": "Петя 10.10. 2 мес"})]
    r = parse_sheet(rows, TODAY)
    assert r[0]["written_off"] and "строка 3" in r[0]["note"] and not r[1]["written_off"]


def test_free_slot():
    empty = {"product": "console", "slot1_client": None, "slot2_client": None}
    assert free_slot(empty, None) == 1 and free_slot(empty, 2) == 2
    assert free_slot({**empty, "slot2_client": "Иван"}, 2) == 1
    assert free_slot({"product": "pc", "slot1_client": None}, None) == 1
