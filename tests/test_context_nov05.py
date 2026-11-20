"""05.11, разбор 81 ответа бота, удалённых владельцем за неделю: бот не видел дат и не отличал владельца от себя,
после выдачи «логин и пароль» просил код, код выставлял счёт на «оформлял», на торг и на вопрос про отзыв."""
from datetime import datetime, timedelta, timezone

from services.dialog import brain

P = {"console": {2: 1090, 4: 1790, 6: 2290}, "pc": {1: 400, 4: 1200}}
T = datetime(2026, 11, 2, 15, 40, tzinfo=timezone.utc)


def test_history_has_time_author_and_gap():
    h = [brain.Msg("seller", "DNS 45.90.33.120", T - timedelta(days=109), "owner"),
         brain.Msg("client", "консоль не подключается", T, "client"),
         brain.Msg("seller", "Секунду", T + timedelta(minutes=1), "bot")]
    lines = brain.history_lines(h)
    assert lines == ["[16.07 18:40] Продавец (Максим, владелец): DNS 45.90.33.120", "— перерыв 109 дн. —",
                     "[02.11 18:40] Клиент: консоль не подключается", "[02.11 18:41] Продавец (ты): Секунду"]
    who, text = h[0]   # код читает как раньше
    assert who == "seller" and h[1] == ("client", "консоль не подключается")
    p = brain.user_prompt("2 мес — 1 090₽", None, h, now=T)
    assert "сейчас 02.11.2026 18:40" in p and "— перерыв 109 дн. —" in p
    assert "Клиент: ало" in brain.user_prompt("", None, [("client", "ало")])   # без времени — как раньше
    # до перерыва — только начало сообщения: старый DNS из инструкции владельца бот не видит (стенд 05.11)
    old = [brain.Msg("seller", "💊Для того, чтобы снова вернуть ваш Xbox к жизни, просто сделайте следующее: 1. Нажмите "
                                "кнопку Xbox на контроллере. 2. Выберите «Настройки». 3. DNS основной 45.90.33.120",
                     T - timedelta(days=100), "owner")] + h[1:]
    assert "45.90" not in "\n".join(brain.history_lines(old)) and "45.90" in "\n".join(brain.history_lines(old[:1]))


def test_paid_creds_order_status():
    o = {"status": "paid", "product": "console", "months": 2, "amount": 990, "stock_id": 5}
    assert "код с экрана консоли не нужен" in brain.order_status({**o, "creds": True})
    assert "получить код на экране консоли" in brain.order_status(o)


def test_no_invoice_without_offer_or_on_haggle():
    wait = [("client", "Можем ещё раз подключить профиль?"),
            ("seller", "Понял. Тогда подключим новый профиль — напишите, когда будете у консоли.")]
    assert brain.implied_order(wait + [("client", "У консоли сейчас"), ("client", "24июня оформлял на 6 месяцев.")],
                               P) is None
    assert brain.implied_order([("client", "нужно на 6 месяцев")] + wait + [("client", "Хорошо")], P) is None
    up = ("2 мес — 990 ₽ со скидкой (495 ₽/мес)\n\nЕсли взять дольше — выходит выгоднее:\n"
          "4 мес — 1 690 ₽ со скидкой (422 ₽/мес)\n6 мес — 2 190 ₽ со скидкой (365 ₽/мес)\n\n"
          "Оформляем на 4 или 6 мес или оставим 2?")
    assert brain.implied_order([("client", "2"), ("seller", up), ("client", "2 месяца за 495 рублей")], P) is None
    assert brain.implied_order([("client", "2"), ("seller", up), ("client", "оставим 2")], P)["months"] == 2
    assert brain.implied_order([("client", "6 мес на консоль"), ("seller", "6 мес — 2 290 ₽. Оформляем?"),
                                ("client", "давайте за 2290")], P)["months"] == 6
    assert brain.sums_said("за 495, или 1 090₽") == [495, 1090] and brain.sums_said("на 6 месяцев") == []


def test_price_and_game_answers_not_replaced():
    price = "🎮 Консоль\n2 мес — 1 090₽\n12 мес — 4 290₽"
    q = "А цена выше написанная 4190 за отзыв ?"
    ok = "Да, 4 190 ₽ — это 12 мес со скидкой за отзыв."
    assert brain.ensure_price(ok, q, price) == ok                       # ответ ценой — как есть
    assert brain.ensure_price("", "сколько стоит?", price) == "{прайс}"  # пусто — прайс
    assert brain.ensure_price("6 мес — 2 290 ₽, 12 мес — 4 290 ₽.", "подскажите цены", price) == "{прайс}"
    assert brain.ensure_price("6 мес — 2 290 ₽.", "сколько на 6?", price) == "6 мес — 2 290 ₽."
    assert brain.ensure_price("2 мес — 1090 ₽, 4 мес — 1790 ₽, 6 мес — 2290 ₽", "цены?", price) == "{прайс}"
    assert brain.ensure_price("На консоль или на ПК?", "сколько стоит?", price) == "{прайс}\n\nНа консоль или на ПК?"
    assert not brain.game_reply_bad("Уточню и напишу Вам.") and not brain.game_reply_bad("Ключ пришлю, минуту.")
    for bad in ("", "Цена 2100 ₽", "Хорошо, уточню", "Можем продать за 3200. Оформляем?"):
        assert brain.game_reply_bad(bad), bad


def test_review_question_is_not_review():
    h = [("client", "Интересует на 12 месяцев для Xbox"), ("seller", "12 мес за 4190 сделаем"),
         ("client", "А цена выше написанная 4190 за отзыв ?")]
    assert not brain.review_only(h[-1][1], h)
    assert brain.review_order(h, {"console": {12: 4290}}) is None
    done = h[:2] + [("client", "Написал отзыв")]
    assert brain.review_only(done[-1][1], done) and brain.review_order(done, {"console": {12: 4290}})["months"] == 12


def test_offer_nudge():
    """06.11: на «Оформляем…?» клиент молчит 10 мин — «Интересно данное предложение?)»; ответ на неё — ответ на
    предложение до неё."""
    up = ("6 мес — 2 190 ₽ со скидкой (365 ₽/мес)\n\nЕсли взять дольше — выходит выгоднее:\n12 мес — 4 190 ₽ со скидкой "
          "(349 ₽/мес) + игра в подарок 🎁\n\nОформляем на 12 мес или оставим 6?")
    for t in (up, "6 мес — 2 290 ₽. Оформляем?", "На какой срок оформляем — 2, 4 или 6 мес?"):
        assert brain.OFFER_WAIT.search(t), t
    for t in ("Вас на какой срок интересует?", "Оформляем заказ, минуту.", brain.OFFER_NUDGE, "Оформляем? Или подумаете"):
        assert not brain.OFFER_WAIT.search(t), t
    h = [("client", "Можно приобрести подписку на 6 мес"), ("seller", up), ("seller", brain.OFFER_NUDGE)]
    p = {"console": {6: 2290, 12: 4290}, "pc": {1: 400}}
    assert brain.implied_order(h + [("client", "Давайте 6 месяцев")], p)["months"] == 6
    assert brain.implied_order(h + [("client", "оставим 6")], p)["months"] == 6
    assert brain.implied_order([("client", "6 мес на консоль"), ("seller", "6 мес — 2 290 ₽. Оформляем?"),
                                ("seller", brain.OFFER_NUDGE), ("client", "да")], p)["months"] == 6
    three = ("2 мес — 990 ₽ со скидкой (495 ₽/мес)\n\nЕсли взять дольше — выходит выгоднее:\n"
             "4 мес — 1 690 ₽ со скидкой (422 ₽/мес)\n6 мес — 2 190 ₽ со скидкой (365 ₽/мес)\n\n"
             "Оформляем на 4 или 6 мес или оставим 2?")
    assert brain.upsell_unclear([("client", "2"), ("seller", three), ("seller", brain.OFFER_NUDGE),
                                 ("client", "давайте")]) == "На какой срок оформляем — 2, 4 или 6 мес?"


def test_nov06_hints():
    """06.11: приветствие не в первом сообщении, подключение по режиму автовыдачи."""
    for t in ("Здравствуйте. Хотел бы на год оформить", "Добрый день, а на 4 месяца?", "привет", "Доброе утро"):
        assert brain.HELLO.search(t), t
    for t in ("Хотел бы на год", "А здравствуйте это вы?", "Добро пожаловать"):
        assert not brain.HELLO.search(t), t
    assert "логин и пароль нашего аккаунта" in brain.issue_hint("creds")
    off = brain.issue_hint("off")
    assert "через код с экрана консоли" in off and "не присылаем" in off
    assert brain.issue_hint("???") == off
    paid = {"status": "paid", "product": "console", "months": 2, "amount": 990}
    assert "через код с экрана консоли" in brain.order_status(paid)
    assert "через код" not in brain.order_status({**paid, "product": "personal"})
