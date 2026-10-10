from services.dialog.brain import (BOT_JOKES, BOT_WORDS, drop_repeats, foreign_prices, is_ack, is_duplicate, load_kb,
                                   implied_order, needs_answer, parse_decision, render_reply, user_prompt)

PRICE = "💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽"


def test_render_placeholders():
    out = render_reply("На ПК тоже можно.", PRICE, first=True)
    assert out.startswith("🫶🏻Приветствуем") and "1 200₽" in out and out.endswith("На ПК тоже можно.")
    assert render_reply("Вот цены:\n{прайс}", PRICE).endswith("4 мес — 1 200₽")


def test_parse_decision_unknown_reason():
    d = parse_decision('{"reply": "Сейчас уточню", "handoff": {"reason": "xxx"}, "stage": "вопрос"}')
    assert d.handoff["reason"] == "nonstandard" and d.reply == "Сейчас уточню"
    assert parse_decision('{"reply": "Да", "handoff": null}').handoff is None
    d = parse_decision('{"reply": "Не бот, всё хорошо", "handoff": {"reason": "bot_question"}}')
    assert d.reply in BOT_JOKES   # слово «бот» в шутке — подменяем готовой
    d = parse_decision('{"reply": "Я живой человек 🙂", "handoff": {"reason": "bot_question"}}')
    assert d.reply in BOT_JOKES
    joke = "Просто отвечаем быстро 😄 Вечером пишите — подключим."
    assert parse_decision('{"reply": "%s", "handoff": {"reason": "bot_question"}}' % joke).reply == joke
    assert not BOT_WORDS.search("Работаем без выходных, ботинки не нужны")


def test_foreign_prices():
    prices = {1090, 1200}
    assert foreign_prices("На 4 месяца — 1 200 ₽, скидка 100 ₽", prices) == []
    assert foreign_prices("Могу за 999₽", prices) == []                 # новому: 1090 - 91, в пределах 100
    assert foreign_prices("Могу за 950₽", prices) == [950]              # больше 100 скидки
    assert foreign_prices("Могу за 990₽", prices, new=False) == [990]   # покупавшему скидки нет
    assert foreign_prices("На Ваш аккаунт 4 мес — 4 090₽", prices | {4190}, True, prices) == [4090]  # личный — без скидки


def test_kb_has_no_price_snapshot_or_analytics():
    kb = load_kb("core")
    assert "## Прайс" not in kb and "## Почему не купили" not in kb and "## Частые вопросы" in kb


def test_user_prompt():
    p = user_prompt(PRICE, "Game Pass", [("client", "ало")])
    assert "Клиент: ало" in p and "1 200₽" in p


def test_foreign_prices_without_rub_sign():
    assert foreign_prices("Тогда за 1000 сделаем", {1090}, new=False) == [1000]
    assert foreign_prices("подключим за 5 минут, за 2 месяца", {1090}) == []


def test_drop_repeats_removes_sentence_already_said():
    hist = [("client", "Сложно подключается ?"),
            ("seller", "Нет, всё просто — минут 10. Вам на 2 месяца за 990₽ со скидкой за отзыв? 🌟"),
            ("client", "Какие игры ещё входят в подписку")]
    reply = "500+ игр: FC 26, EA Play, Ubisoft. Вам на 2 месяца за 990₽ со скидкой за отзыв подключаем? 🌟"
    assert drop_repeats(reply, hist) == "500+ игр: FC 26, EA Play, Ubisoft."


def test_drop_repeats_keeps_short_new_and_all_repeated():
    hist = [("seller", "Хорошо, вечером пишите — буду на связи."), ("client", "Ок")]
    assert drop_repeats("Хорошо, вечером пишите — буду на связи.", hist) == "Хорошо, вечером пишите — буду на связи."
    assert drop_repeats("Хорошо.\nПодписка на 4 мес — 1 790₽", hist) == "Хорошо.\nПодписка на 4 мес — 1 790₽"


def test_render_personal_price_as_own_paragraph():
    out = render_reply("Срок от 4 месяцев: {прайс_личный}. Скидка за отзыв тоже действует.", "P",
                       personal="Личный\n4 мес — 4 190₽")
    assert out == "Срок от 4 месяцев:\n\nЛичный\n4 мес — 4 190₽\n\nСкидка за отзыв тоже действует."


def test_render_gift_list():
    out = render_reply("Вот из чего можно выбрать: {подарки}", "P", gifts="🎇 Выберите\n💎Terraria")
    assert out == "Вот из чего можно выбрать:\n\n🎇 Выберите\n💎Terraria"


def test_terms_hint_from_price():
    from services.dialog.brain import terms_hint
    price = "🎮 XBOX ULTIMATE дом консоль\n2 мес — 1 090₽\n4 мес — 1 790₽\n10 мес — 3 590₽ 🎁\n\n💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽\n10 мес — 1 800₽"
    h = terms_hint(price)
    assert "СРОКИ: 4, 10 мес — есть и для консоли, и для ПК" in h and "Только для ПК: 1 мес" in h and "Только для консоли: 2 мес" in h
    assert terms_hint("🎮 консоль\n2 мес — 1 090₽") == ""


def test_guard_reply():
    from services.dialog.brain import GIFT_FIRST_ONLY, guard_reply
    assert guard_reply("Да, от 10 мес игра в подарок. Выберите: {подарки}", new=False) == GIFT_FIRST_ONLY
    price = "🎮 консоль\n3 мес — 1 590₽\n20 мес — 6 890₽\n\n💻 ПК\n20 мес — 3 900₽"
    assert guard_reply("20 мес — 6 890₽, это для консоли. На консоль или на ПК нужно?", True, price) == "На консоль или на ПК нужно?"
    assert guard_reply("3 мес — 1 590₽, для консоли. На консоль или на ПК?", True, price) == "3 мес — 1 590₽, для консоли."
    assert guard_reply("Да, от 10 месяцев — одна игра из списка, от 14 — ещё и участие в розыгрыше.",
                       new=False) == GIFT_FIRST_ONLY
    assert guard_reply("Да, туда же. 6 мес — 2 290₽.", True) == "6 мес — 2 290₽."
    assert parse_decision('{"reply": "Нет, отвечаем быстро 😄", "handoff": {"reason": "bot_question"}}').reply == "Отвечаем быстро 😄"
    assert guard_reply("Гарантия на весь срок. Можем сперва подключить, оплатите после.", True) == "Гарантия на весь срок."
    p4 = "🎮 консоль\n4 мес — 1 790₽\n10 мес — 3 590₽\n\n💻 ПК\n4 мес — 1 200₽\n10 мес — 1 800₽"
    assert guard_reply("10 мес — 3 590₽, игра в подарок 🎁", True, p4, "10 месяцев сколько?") == "На консоль или на ПК?"
    assert guard_reply("10 мес — 3 590₽ 🎁 Подключаем?", True, p4, "10 мес на консоль") == "10 мес — 3 590₽ 🎁 Подключаем?"
    assert guard_reply("На Ваш аккаунт 4 мес — 4 190₽.", True, p4, "можно на мой аккаунт?") == "На Ваш аккаунт 4 мес — 4 190₽."
    assert parse_decision('{"reply": "Отвечаем вручную 🙂", "handoff": {"reason": "bot_question"}}').reply in BOT_JOKES
    assert guard_reply("6 мес — 2 290₽. Подключаем?", True) == "6 мес — 2 290₽. Подключаем?"


def test_first_reply_does_not_repeat_tail_question():
    out = render_reply("Есть, Halo 3 в подписке. Вас на какой срок интересует?", "P", first=True)
    assert out.count("на какой срок") == 1 and out.endswith("Есть, Halo 3 в подписке.")


def test_guard_human_bank_russian():
    from services.dialog.brain import PAY_BANK_LATER, RU_UNKNOWN, guard_reply
    assert guard_reply("Нет, отвечаем вручную.", True) in BOT_JOKES and guard_reply("Да, это я.", True) in BOT_JOKES
    assert guard_reply("Сбер. Реквизиты пришлю позже.", True) == "Реквизиты пришлю позже."
    assert guard_reply("Сбер.", True) == PAY_BANK_LATER
    found = {"Starfield": [{"title_en": "Starfield", "ru": False}]}
    assert guard_reply("Да, есть. Русский язык в Starfield есть.", True, found=found) == f"Да, есть. {RU_UNKNOWN}"
    ok = {"FC 26": [{"title_en": "FC 26", "ru": True}]}
    assert guard_reply("Есть, с русскими субтитрами.", True, found=ok) == "Есть, с русскими субтитрами."


def test_first_reply_keeps_joke_drops_tail_only():
    out = render_reply("Просто отвечаем быстро и без воды 😄 Вас на какой срок интересует?", "P", first=True)
    assert out.endswith("Просто отвечаем быстро и без воды 😄") and out.count("на какой срок") == 1
    out = render_reply("3 мес — 1 590₽. Для новых клиентов скидка за отзыв 🌟 Вас на какой срок интересует?", "P", first=True)
    assert out.endswith("3 мес — 1 590₽.")


def test_is_ack():
    for t in ("Ок", "Конечно", "Спасибо большое", "Все понял, спасибо", "👍", "Хорошо, спасибо!", "Лады"):
        assert is_ack(t), t
    for t in ("Жду", "Ок, а сколько стоит?", "По какому коду?", "Оплачиваю", "<image>", ""):
        assert not is_ack(t), t


def test_needs_answer():
    done = [("seller", "Пожалуйста! Если что-то понадобится — пишите."), ("client", "Конечно")]
    assert needs_answer(done) is False                                   # прощание после прощания — молчим
    assert needs_answer([("client", "Сколько атомик?"), ("seller", "Уточню и напишу")]) is False
    assert needs_answer([("seller", "Подключаем?"), ("client", "Да")]) is None  # ответ на вопрос — решает LLM
    assert needs_answer([("seller", "Готово"), ("client", "А PS Plus есть?")]) is True
    assert needs_answer([("seller", "Готово"), ("client", "Жду")]) is None
    # «ок» после названной цены — согласие, отвечаем (09.10 бот молчал); после реквизитов — молчим
    assert needs_answer([("seller", "3 мес — 1 590 ₽."), ("client", "ок")]) is None
    assert needs_answer([("seller", "Номер: +7…\nСумма: 1590 ₽\nБанк: Т-Банк"), ("client", "ок")]) is False


PRICES = {"console": {2: 1090, 3: 1590, 4: 1790, 12: 4290}, "pc": {1: 400, 4: 1200},
          "personal": {4: 4190, 6: 4890}}


def test_implied_order():
    # чат 09.10: «давай 3 месяца» / «ок» после цены / «ну и?» — заказ кодом (3 мес есть только у консоли)
    base = [("client", "что консоль или пк?"), ("seller", "Консоль — … ПК — … Что интересует?")]
    want = {"product": "console", "months": 3, "price": 1590}
    assert implied_order(base + [("client", "давай 3 месяца")], PRICES) == want
    quoted = base + [("client", "давай 3 месяца"), ("seller", "3 мес — 1 590 ₽.")]
    assert implied_order(quoted + [("client", "ок")], PRICES) == want
    assert implied_order(quoted + [("client", "ок"), ("client", "ну и?")], PRICES) == want
    assert implied_order(quoted + [("client", "присылай уже")], PRICES) == want
    # 4 мес — и консоль, и ПК: без явного выбора не угадываем; с «на пк» — ПК; личный — только если говорил о нём
    assert implied_order([("client", "давай 4 месяца")], PRICES) is None
    assert implied_order([("client", "мне на пк"), ("seller", "…"), ("client", "давай 4 месяца")], PRICES)["product"] == "pc"
    assert implied_order([("client", "можно на мой аккаунт?"), ("seller", "…"), ("client", "беру 4 мес")],
                         PRICES)["product"] == "personal"
    # не согласие / вопрос / прайс целиком — решает LLM
    assert implied_order(quoted + [("client", "дорого, подумаю")], PRICES) is None
    assert implied_order(quoted + [("client", "да, а игры какие есть?")], PRICES) is None
    assert implied_order([("seller", "2 мес — 1 090₽\n3 мес — 1 590₽"), ("client", "ок")], PRICES) is None


def test_is_duplicate():
    said = ["Atomic Heart продаём отдельно, цену уточню — сейчас напишу.", "Да"]
    assert is_duplicate("Atomic Heart продаём отдельно, цену уточню — сейчас напишу.", said)
    assert is_duplicate("Пожалуйста! Если что-то понадобится — пишите 🙂", ["Пожалуйста! Если что-то понадобится — пишите."])
    assert not is_duplicate("Да", said)                                  # короткие повторы допустимы
    assert not is_duplicate("Far Cry 5 есть в подписке.", said)


def test_discount_nudge_triggers():
    from services.dialog.brain import HESITATE, PRICE_TAIL, shows_price
    price = "🎮 XBOX ULTIMATE дом консоль\n2 мес — 1 090₽"
    assert shows_price(f"Приветствуем\n\n{price}\n\n{PRICE_TAIL}", price)
    assert shows_price(f"Вот цены:\n\n{price}", price)
    assert not shows_price("2 мес — 1 090 ₽, для Вас со скидкой за отзыв — 990 ₽", price)
    assert not shows_price("Far Cry 5 есть в подписке", price)
    assert HESITATE.search("Дороговато что-то") and HESITATE.search("я подумаю") and not HESITATE.search("Беру на 2")


def test_discount_decline_and_solo():
    from services.dialog.brain import DISCOUNT_DECLINE, SOLO_Q
    for t in ("Давайте пока без скидки", "скидка не нужна", "не надо скидку", "отзыв не буду оставлять"):
        assert DISCOUNT_DECLINE.search(t), t
    for t in ("мне нужна скидка", "без проблем", "Скидка есть?"):
        assert not DISCOUNT_DECLINE.search(t), t
    for t in ("Нужно будет каждый раз входить в ваш аккаунт?", "а при каждом включении заходить в ваш акк надо?",
              "Заходить постоянно не придётся?"):
        assert SOLO_Q.search(t), t
    for t in ("Как входить в аккаунт?", "каждый раз платить?", "Можно на ПК заходить?"):
        assert not SOLO_Q.search(t), t


def test_bot_ask_and_requisites_above():
    from services.dialog.brain import BOT_ASK, SAME_REQUISITES
    for t in ("Вы бот?", "это бот отвечает?", "ты живой?", "Отвечаете как бот", "можно живого человека?"):
        assert BOT_ASK.search(t), t
    for t in ("Подписка работает?", "Ботинки", "А вы работаете?", "человек 2 на одной учётке?"):
        assert not BOT_ASK.search(t), t
    assert SAME_REQUISITES.search("Реквизиты выше — как оплатите, пришлите скрин.")


def test_readable_attachments():
    from services.dialog.brain import readable
    assert readable("<image>", has_images=True) == ""
    assert readable("<image>") == "[фото]"
    assert readable("Вот скрин / <image>", has_images=True) == "Вот скрин"
    assert readable("<voice> / Слышно?") == "[голосовое] / Слышно?"
    assert readable(None) == ""


def test_upsell():
    from services.dialog.brain import upsell_targets, upsell_text
    console = {2: 1090, 3: 1590, 4: 1790, 6: 2290, 8: 2890, 10: 3590, 12: 4290, 13: 4590, 14: 5090, 15: 5390, 20: 6890}
    assert upsell_targets("console", 2, console) == [4, 6]
    assert upsell_targets("console", 6, console) == [12]
    assert upsell_targets("console", 12, console) == [20]
    assert upsell_targets("console", 20, console) == []
    assert upsell_targets("pc", 1, {1: 400, 4: 1200, 10: 1800, 20: 3900}) == [4]
    assert upsell_targets("personal", 9, {4: 1, 6: 1, 9: 1, 11: 1, 12: 1, 13: 1, 14: 1}) == [12]
    rows = {m: {"price": p, "gifts": int(m >= 10), "raffle": m >= 14} for m, p in console.items()}
    t = upsell_text(6, rows, [12], 100)
    assert t.startswith("6 мес — 2 190 ₽ со скидкой (365 ₽/мес)")
    assert "12 мес — 4 190 ₽ со скидкой (349 ₽/мес) + игра в подарок 🎁" in t and t.endswith("оставим 6?")
    t = upsell_text(12, rows, [20], 0)
    assert "20 мес — 6 890 ₽ (344 ₽/мес) + участие в розыгрыше" in t
    t = upsell_text(2, rows, [4, 6], 0)
    assert "4 мес — 1 790 ₽ (448 ₽/мес)" in t and "6 мес — 2 290 ₽ (382 ₽/мес)" in t and t.endswith("оформляем?")


def test_upsell_choice():
    from services.dialog.brain import implied_order, upsell_choice, upsell_text
    assert upsell_choice("Давайте 4", [2, 4, 6]) == 4
    assert upsell_choice("Нет, оставим 2", [2, 4, 6]) == 2
    assert upsell_choice("нет", [2, 4, 6]) == 2
    assert upsell_choice("да", [6, 12]) == 12
    assert upsell_choice("да", [2, 4, 6]) is None          # два варианта — пусть решает LLM
    assert upsell_choice("давайте 8", [2, 4, 6]) is None   # не из предложенных
    assert upsell_choice("нет, подумаю", [6, 12]) is None
    rows = {m: {"price": p, "gifts": 0, "raffle": False} for m, p in {2: 1090, 4: 1790, 6: 2290}.items()}
    prices = {"console": {2: 1090, 4: 1790, 6: 2290}, "pc": {1: 400, 4: 1200}}
    h = [("client", "беру 2 мес на консоль"), ("seller", upsell_text(2, rows, [4, 6], 100)), ("client", "давайте 6")]
    assert implied_order(h, prices) == {"product": "console", "months": 6, "price": 2290}
    h[-1] = ("client", "Нет, оставим 2")
    assert implied_order(h, prices)["months"] == 2
    assert implied_order([("seller", "6 мес — 2 290 ₽"), ("client", "Продлите на 6 месяцев")],
                         prices)["months"] == 6


def test_ask_go():
    from services.dialog.brain import ASK_GO, _sentences
    r = "20 мес — 6 890 ₽, для Вас со скидкой за отзыв — 6 790 ₽. Игра в подарок. Оплачиваете?"
    assert " ".join(s for s in _sentences(r) if not ASK_GO.search(s)).endswith("Игра в подарок.")
    assert not ASK_GO.search("Оплачиваете с карты Сбера — подойдёт любой банк.")
