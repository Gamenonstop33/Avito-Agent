"""07.11, решения владельца: «за 4 взял бы» — цена в тысячах, скидка постоянным и торг ниже скидки."""
from services.dialog import brain

PRICES = {"console": {4: 1790, 6: 2290, 12: 4290}, "pc": {4: 1200}, "personal": {12: 5990}}


def terms(text):
    return [int(m.group(1)) for m in brain.BARE_NUM.finditer(text)]


def test_za_n_with_buy_verb_is_price():
    # 06.11, PREMIUM: после «12 мес — 4 290 ₽» на «Вот за 4 взял бы» бот выставил «4 мес — 1 790 ₽»
    assert terms("Вот за 4 взял бы") == []
    assert terms("за 3,5 отдадите?") == []
    assert terms("за 4к возьму") == []
    assert brain.implied_order([("seller", "12 мес — 4 290 ₽. Оформляем?"), ("client", "Вот за 4 взял бы")],
                               PRICES) is None


def test_za_n_question_and_plain_terms_stay():
    assert terms("а за 6 сколько?") == [6]
    assert terms("давайте 4") == [4]
    assert terms("давай 12") == [12]


def test_discount_named():
    assert brain.discount_named([("seller", "12 мес — 4 290 ₽, для Вас со скидкой за отзыв — 4 190 ₽.")], PRICES)
    assert brain.discount_named([("seller", "12 мес за 4 190 сделаем. Оформляем?")], PRICES)
    assert not brain.discount_named([("seller", "Продлеваем — 12 мес — 4 290 ₽. Оформляем?")], PRICES)
    # прайс с «скидку за отзыв» в хвосте — цены без скидки, не в счёт
    assert not brain.discount_named([("seller", "2 мес — 1 090₽\n12 мес — 4 290₽\nДля новых клиентов делаем "
                                                "скидку за отзыв 🌟")], PRICES)


def test_returning_status_allows_discount_on_request():
    text = brain.client_status(False)
    assert "100 ₽" in text and "сам не предлагай" in text
    assert "только при первой покупке" in text   # подарок и розыгрыш — по-прежнему только первая покупка


def test_pause_note_keeps_silence_on_unclear():
    assert "reply пустой" in brain.PAUSE_NOTE and "я спать" in brain.PAUSE_NOTE
