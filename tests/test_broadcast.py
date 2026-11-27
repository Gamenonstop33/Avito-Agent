from datetime import datetime, timedelta, timezone

from services.broadcast.audience import Filter, describe, eta_days
from services.broadcast.worker import next_send_at

MSK = timezone(timedelta(hours=3))


def at(h, m=0):
    return datetime(2026, 9, 29, h, m, tzinfo=MSK)


def test_spread_evenly_over_day():
    assert next_send_at(at(8, 0), None, 0) == at(8, 0)                      # первое за день — сразу
    # в 10:00 отправлено 10 из 40, осталось 10 часов на 30 сообщений → раз в 20 минут
    assert next_send_at(at(10, 0), at(10, 0), 10) == at(10, 20)
    assert next_send_at(at(15), at(14, 50), 40) is None                    # квота исчерпана
    assert next_send_at(at(21), at(19, 50), 5) is None                     # ночью не шлём


def test_filter_validation_and_description():
    f = Filter.from_dict({"bought": "no", "interest": ["core", "xxx"], "wrote_within_days": 60.0,
                          "max_recipients": "много", "item_contains": "  12 мес "})
    assert f.interest == ["core"] and f.wrote_within_days == 60 and f.max_recipients is None
    text = describe(f)
    assert "ещё ничего не покупали" in text and "подписка Game Pass" in text and "«12 мес»" in text
    assert describe(Filter()) == "• все клиенты"


def test_eta():
    assert eta_days(40) == 1 and eta_days(41) == 2


def test_round_by_topic():
    from services.broadcast.audience import round_pick
    assert round_pick([1, 2, 3], {}) == ([1, 2, 3], 1, 0)            # первый круг — все
    assert round_pick([1, 2, 3], {1: 1}) == ([2, 3], 1, 1)           # 1 уже получил в этом круге
    assert round_pick([1, 2, 3], {1: 1, 2: 1, 3: 1}) == ([1, 2, 3], 2, 0)   # круг пройден — новый
    assert round_pick([1, 2], {1: 2, 2: 1, 9: 5}) == ([2], 2, 1)


def test_purchase_starts_split_by_gap():
    from services.broadcast.purchases import purchase_starts
    tpl, review = "1) Нажмите кнопку Xbox на джойстике и ведите вправо", "Если все ок оставьте отзыв)"
    msgs = [(at(10), "14 мес за 4750"), (at(10, 5), tpl), (at(11), review),          # покупка 1
            (at(10) + timedelta(days=2), review),                                    # та же покупка, напоминание
            (at(10) + timedelta(days=150), tpl), (at(10) + timedelta(days=150, hours=1), None)]   # продлил
    assert purchase_starts(msgs) == [at(10, 5), at(10) + timedelta(days=150)]
    assert purchase_starts([(at(10), "добрый день")]) == []


def test_parse_term():
    from services.broadcast.purchases import parse_term
    assert parse_term({"product": "console", "months": 14}) == ("console", 14)
    assert parse_term({"product": "pc", "months": 1.0}) == ("pc", 1)
    assert parse_term({"product": "other", "months": 3}) == ("other", None)        # не подписка — без срока
    assert parse_term({"product": "console", "months": 99}) == ("console", None)
    assert parse_term({"product": "xbox", "months": "12"}) == (None, None)
    assert parse_term({"purchase": False, "product": "console", "months": 10}) == ("none", None)   # поддержка


def test_renew_text_by_moment_of_sending():
    from services.broadcast.audience import renew_text
    now = at(12)
    ended = renew_text(at(9) - timedelta(days=3), 12, now)
    assert "на 12 мес. закончилась 26.09" in ended
    assert "заканчивается 04.10" in renew_text(at(9) + timedelta(days=5), 12, now)
    assert renew_text(now - timedelta(days=31), 12, now) is None     # закончилась давно — не продление
    assert renew_text(now + timedelta(days=8), 12, now) is None      # ещё рано
    assert renew_text(now, None, now) is None                        # срок неизвестен — не пишем


def test_parse_creds():
    from services.broadcast.purchases import parse_creds
    assert parse_creds("abc@outlook.com:Pass1234") == ("abc@outlook.com", "Pass1234", None)
    assert parse_creds('"2m\nabc@outlook.com\n7eRZr83M"') == ("abc@outlook.com", "7eRZr83M", None)
    assert parse_creds("Почта: abc@outlook.com\nПароль: Qwe12345\nРезервная почта: rr@mail.ru") == \
        ("abc@outlook.com", "Qwe12345", "rr@mail.ru")
    assert parse_creds("abc@outlook.com 💚 Qwe12345") == ("abc@outlook.com", "Qwe12345", None)
    assert parse_creds("abc@outlook.com\nrr@mail.ru") == ("abc@outlook.com", None, "rr@mail.ru")
    assert parse_creds("Учетная запись- abc@outlook.com\nРегион на приставке - Аргентина") == ("abc@outlook.com", None, None)
    assert parse_creds("Инструкция для активации ключа XBOX … abc@outlook.com") is None
    assert parse_creds("Пришлите фото") is None
