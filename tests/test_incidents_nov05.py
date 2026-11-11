"""05.11, PREMIUM: клиентам с действующей подпиской (до 06.04 и 11.12) бот предлагал продление и прайс.
«почему» ловилось как «почём?», «я покупал на 2 месяца» — как выбор срока, «Спасибо большое, все работает» — не «спасибо»."""
from datetime import datetime, timedelta, timezone

from services.dialog import brain


def test_pochemu_is_not_price_question():
    q = "Здравствуйте почему у меня нету облачного гейминга"
    assert not brain.PRICE_Q.search(q) and not brain.PRICE_ASK.search(q)
    assert brain.PRICE_Q.search("почем 6 мес?") and brain.PRICE_ASK.search("почём подписка")


def test_thanks_it_works_is_ack():
    assert brain.is_ack("Спасибо большое, все работает")
    assert brain.needs_answer([("client", "А эти настройки где делать?"), ("seller", "В своём профиле."),
                               ("client", "Спасибо большое, все работает")]) is False
    assert not brain.is_ack("не работает") and not brain.is_ack("все работает?")


def test_buy_intent_past_purchase_is_not_intent():
    for t in ("Так у меня еще должна действовать я покупал на 2 месяца", "Я только 2 недели назад у вас продлил",
              "У меня еще действует", "Можно это исправить", "продлевал в октябре"):
        assert not brain.BUY_INTENT.search(t), t
    for t in ("хочу продлить на 2 месяца", "сколько стоит продление", "а можно еще на 2 месяца", "можно продлить?",
              "давайте ещё 2 сверху", "нужно на 4 месяца", "почём 6 мес?"):
        assert brain.BUY_INTENT.search(t), t


def test_active_hint_has_end_date():
    paid = datetime(2026, 10, 6, 13, 7, tzinfo=timezone.utc)
    h = brain.ACTIVE_HINT.format(name="консоль", months=6, paid=paid, end=paid + timedelta(days=182))
    assert "действует до 06.04.2027" in h and "оплачена 06.10.2026" in h and "прайс сам не предлагай" in h


def test_renewed_two_weeks_ago_is_not_an_order():
    """Стенд 05.11: «продлил 2 недели назад» — CONSENT ловил «продл…», «2 недели» — как срок → счёт на 2 мес."""
    prices = {"console": {2: 1090, 4: 1790, 6: 2290}, "pc": {1: 690, 4: 1990}}
    hist = [("client", "А эти настройки делать в профиле который вы мне сделали или в моем?"),
            ("seller", "В своём профиле."), ("client", "Я только 2 недели назад у вас продлил")]
    assert brain.implied_order(hist, prices) is None
    assert brain.implied_order([("seller", "6 мес — 2 290 ₽. Оформляем?"), ("client", "да, продлеваем")], prices)


def test_first_reply_without_price_for_active_client():
    price = "🎮 XBOX ULTIMATE\n2 мес — 1 090₽"
    hello = brain.render_reply("", price, first=True, new=False, sell=False)
    assert brain.GREETING in hello and brain.HOW_HELP in hello and "1 090" not in hello
    asked = brain.render_reply("Облачный гейминг — только на личном аккаунте.", price, first=True, new=False, sell=False)
    assert "1 090" not in asked and "Облачный" in asked
    assert "1 090" in brain.render_reply("", price, first=True, new=False)   # купить пришёл — как раньше
    assert "1 090" in brain.render_reply("Вот цены: {прайс}", price, first=True, new=False, sell=False)   # LLM сама
