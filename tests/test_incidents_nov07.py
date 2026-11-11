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


def test_loyal_discount_only_when_asked():
    # 07.11 вечер, PREMIUM: «Можно продлить подписку на месяц?» → «На 2 месяца» — бот сам дал скидку постоянному
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 7, 16, 57, tzinfo=timezone.utc)
    old = [brain.Msg("seller", "Для новых клиентов делаем скидку за отзыв 🌟 / 3 мес за 1390 сделаем",
                     t0 - timedelta(days=180), "owner")]
    talk = [brain.Msg("client", "Можно продлить подписку на месяц?", t0, None),
            brain.Msg("seller", "На месяц подписки нет — минимальный срок 2 мес.", t0, "bot"),
            brain.Msg("client", "На 2 месяца", t0, None)]
    assert not brain.discount_asked(old + talk)   # скидка владельца полгода назад — другой разговор
    for ask in ("А скидку сделаете?", "дороговато", "а подешевле никак?", "Вот за 4 взял бы", "за 3,5 отдадите?",
                "уступите немного?", "год стоит 500 рублей, оформляй"):
        assert brain.discount_asked(talk + [brain.Msg("client", ask, t0, None)]), ask
    for plain in ("сколько за 2 месяца?", "давайте без скидки", "скиньте реквизиты", "На 6 месяцев"):
        assert not brain.discount_asked(talk + [brain.Msg("client", plain, t0, None)]), plain
    assert brain.discount_asked(talk + [brain.Msg("seller", "2 мес за 990 сделаем", t0, "owner")])
    assert not brain.discount_asked(talk + [brain.Msg("seller", "2 мес за 990 сделаем", t0, "bot")])
    # не просил — цена минус 100 ₽ недопустима, просил — можно
    prices = {1090, 1490}
    assert brain.foreign_prices("2 мес — 1 090 ₽, для Вас со скидкой — 990 ₽", prices, False) == [990]
    assert brain.foreign_prices("2 мес — 1 090 ₽, для Вас со скидкой — 990 ₽", prices, True) == []


GIFTS = ("🎇 Выберите из списка игру в подарок🎇\n\n💎OUTLAST: BUNDLE OF TERROR\n💎Terraria\n💎Batman: Arkham Knight\n"
         "💎PAYDAY 2: CRIMEWAVE EDITION\n💎Metro 2033 Redux")


def test_gift_ask_which_game():
    for q in ("А какая игра идёт в подарок?", "Какая игра в подарок ?", "Какие игры в подарок?",
              "какую игру можно выбрать в подарок", "что за подарок?", "Какую игру дарите?"):
        assert brain.GIFT_ASK.search(q), q
    assert not brain.GIFT_ASK.search("Какая игра есть в подписке?")


def test_gift_label_replaces_retyped_list():
    # стенд 07.11: нейросеть перепечатала список одной строкой — уходит текст владельца как есть
    llm = ("Выберите из списка игру в подарок 🎁 💎OUTLAST: BUNDLE OF TERROR 💎Terraria 💎Batman: Arkham Knight "
           "💎Metro 2033 Redux Игры активируются на ваш аккаунт.")
    assert brain.gift_label(llm, GIFTS) == "{подарки}\n\nИгры активируются на ваш аккаунт."
    assert brain.gift_label("Да, положен! Список:\n💎Terraria\n💎Metro 2033 Redux\n💎PAYDAY 2: CRIMEWAVE EDITION",
                            GIFTS) == "Да, положен!\n\n{подарки}"
    one = "Например, Terraria или Metro 2033 Redux — пришлите почту и пароль."
    assert brain.gift_label(one, GIFTS) == one


def test_gift_extra_drops_template_repeats():
    gifts = GIFTS + "\n\n🔸 Игры активируются на ваш аккаунт и остаются навсегда.\n🔸 Выдача  в течении 5–10 минут."
    out = brain.gift_extra("{подарки}\n\nИгры активируются на ваш аккаунт и остаются навсегда. Выдача в течение 5–10 "
                           "минут. Выбрали — пришлите почту и пароль от аккаунта.", gifts)
    assert out.startswith("{подарки}") and "активируются" not in out and "Выдача" not in out
    assert "пришлите почту и пароль" in out
    assert brain.gift_extra("Да, положен! {подарки}", gifts) == "Да, положен! {подарки}"


def test_owner_pause_10_offer_nudge_5():
    # 08.11, владелец: «5 мин паузы после моего сообщения мало» и «через 5 мин пишет интересно данное предложение?»
    from datetime import timedelta
    from services.dialog import night, worker
    assert night.OWNER_IDLE == timedelta(minutes=10)
    assert worker.OFFER_AFTER == timedelta(minutes=5)


def test_pc_only_term_needs_platform():
    # 10.11, PREMIUM: «1 месяц» — код сам решил «ПК», дал условия аренды и счёт, а клиенту нужно на Xbox One
    prices = {"console": {2: 1090, 6: 2290}, "pc": {1: 400, 4: 1200}, "personal": {4: 4190}}
    assert brain.term_product(1, "1 месяц", prices) is None
    assert brain.term_product(1, "на пк 1 месяц", prices) == "pc"
    assert brain.term_product(1, "1 месяц", prices, known="pc") == "pc"
    assert brain.term_product(2, "2 месяца", prices) == "console"   # срок только консоли — как раньше
    hist = [("client", "Хочу купить подписку на свой ак"), ("seller", "1 мес — только на ПК, 400 ₽. Оформляем?"),
            ("client", "Да, 1 мес")]
    assert brain.implied_order(hist, prices) is None
    assert brain.PC_ONLY_ASK.format(n=1, low=2).startswith("1 мес — только на ПК")


def test_own_account_question():
    for q in ("На личный ак будет подписка же ?", "На свой ак", "на мой акк будет?", "на своём аккаунте будет работать?"):
        assert brain.OWN_ACC_Q.search(q), q
    assert brain.OWN_ACC_FIRM.search("Мне нужно именно на личный аккаунт")
    assert not brain.OWN_ACC_FIRM.search("На личный ак будет подписка же ?")
    assert not brain.OWN_ACC_Q.search("а как вы подключаете?")


def test_game_number_is_not_term():
    # 11.11, PREMIUM: «И интересует покупка гта 6» после допродажи 4/6 — ушёл счёт на 6 мес
    assert brain.chosen_term("И интересует покупка гта 6") is None
    assert not list(brain.BARE_NUM.finditer("fc 25 есть? и пс 5"))
    assert brain.chosen_term("давайте 6") == 6 and brain.chosen_term("на 6 месяцев") == 6
    prices = {"console": {2: 1090, 4: 1790, 6: 2290}, "pc": {1: 400}}
    hist = [("client", "2 месяца"), ("seller", "2 мес — 990 ₽ со скидкой\n\nЕсли взять дольше — выходит выгоднее:\n"
                                              "4 мес — 1 690 ₽\n6 мес — 2 190 ₽\n\nОформляем на 4 или 6 мес или оставим 2?"),
            ("client", "И интересует покупка гта 6")]
    assert brain.implied_order(hist, prices) is None


def test_gift_guard_keeps_answer_for_returning():
    # 11.11, GameNonStop: на «А на личный аккаунт нельзя активировать?» весь ответ заменился «подарок только при первой»
    reply = ("Подключаем через дополнительный аккаунт, играете со своего профиля. Если нужно именно на личный — цена "
             "другая: {прайс_личный} Подарка и скидки на личный аккаунт нет.")
    out = brain.guard_reply(reply, new=False)
    assert "Подключаем через дополнительный" in out and "Подарка и скидки на личный аккаунт нет" in out
    out = brain.guard_reply("Можно на личный — от 4 мес. И игра в подарок 🎁 при оформлении от 10 мес.", new=False)
    assert "Можно на личный" in out and "в подарок" not in out
    assert brain.guard_reply("При покупке от 14 мес — розыгрыш GTA 6 и игра в подарок.", new=False) == \
        brain.GIFT_FIRST_ONLY


def test_firm_own_account_and_questions():
    assert brain.OWN_ACC_FIRM.search("Я хочу ее активировать на свой личный аккаунт")
    assert not brain.OWN_ACC_FIRM.search("На личный ак будет подписка же ?")
    assert brain.asks_question("Что за розыгрыш") and brain.asks_question("На месяц какая цена")
    assert not brain.asks_question("я спать")


def test_assist_code_words():
    # 11.11, Макс: «пусть будет слово ассистент, остановка: стоп ассистент»
    assert brain.assist_cmd("ассистент") == "on" and brain.assist_cmd("Ассистент!") == "on"
    assert brain.assist_cmd("стоп ассистент") == "off" and brain.assist_cmd("Стоп, ассистент.") == "off"
    for t in ("ассистент подключит", "стоп", "наш ассистент ответит", None):
        assert brain.assist_cmd(t) is None, t
    import re
    m = re.match(r"(?i)^\W*(стоп\W+)?ассистент\b.*?(u2i-[\w~-]+)",
                 "стоп ассистент https://www.avito.ru/profile/messenger/channel/u2i-t9TbTdHy_5q9GqPm~x2G1Q", re.S)
    assert m and m.group(1) and m.group(2) == "u2i-t9TbTdHy_5q9GqPm~x2G1Q"
