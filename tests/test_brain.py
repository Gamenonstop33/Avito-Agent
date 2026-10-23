from services.dialog.brain import (BOT_JOKES, BOT_WORDS, drop_repeats, foreign_prices, is_ack, is_duplicate, load_kb,
                                   implied_order, needs_answer, parse_decision, render_reply, user_prompt)

PRICE = "💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽"


def test_render_placeholders():
    out = render_reply("На ПК тоже можно. {прайс}", PRICE, first=True)
    assert out.startswith("🫶🏻Приветствуем") and out.count("1 200₽") == 1
    assert out.index("На ПК тоже можно.") < out.index("1 200₽") and out.endswith("на какой срок интересует?")
    assert render_reply("Вот цены:\n{прайс}", PRICE).endswith("4 мес — 1 200₽")


def test_first_reply_answer_first_price_only_when_asked():
    """19.10: «GTA 5 есть в гейм пассе?» — шапка и весь прайс, ответ в самом конце; «подскажите цены» — прайс дважды."""
    out = render_reply("GTA 5 в подписке нет. Можем продать её отдельно — интересно?", PRICE, first=True)
    assert "1 200₽" not in out and out.endswith("интересно?") and out.startswith("🫶🏻Приветствуем")
    out = render_reply("Для Xbox One подходит. {прайс}", PRICE, first=True)
    assert out.count("1 200₽") == 1 and out.index("Xbox One") < out.index("1 200₽")
    assert "1 200₽" in render_reply("", PRICE, first=True)                 # «Xbox», «привет» — шапка и прайс
    assert render_reply("{прайс_личный}\n\nИли на личный: {прайс_личный}", "P", personal="L").count("L") == 1


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
    assert out.count("на какой срок") == 1 and "Есть, Halo 3 в подписке.\n\nP\n\n" in out   # спросил срок — с прайсом


def test_guard_human_bank_russian():
    from services.dialog.brain import PAY_BANK_LATER, RU_UNKNOWN, guard_reply
    assert guard_reply("Нет, отвечаем вручную.", True) in BOT_JOKES and guard_reply("Да, это я.", True) in BOT_JOKES
    assert guard_reply("Сбер. Реквизиты пришлю позже.", True) == "Реквизиты пришлю позже."
    assert guard_reply("Сбер.", True) == PAY_BANK_LATER
    found = {"Starfield": [{"title_en": "Starfield", "ru": False}]}
    assert guard_reply("Да, есть. Русский язык в Starfield есть.", True, found=found,
                       latest="а на русском?") == f"Да, есть. {RU_UNKNOWN}"
    # о языке не спрашивали — без «точно не скажу» (стенд 22.10)
    assert guard_reply("Да, есть. Русский язык в Starfield есть.", True, found=found) == "Да, есть."
    assert guard_reply("Starfield есть, на консоли и ПК, с русскими субтитрами.", True, found=found) == \
        "Starfield есть, на консоли и ПК."
    ok = {"FC 26": [{"title_en": "FC 26", "ru": True}]}
    assert guard_reply("Есть, с русскими субтитрами.", True, found=ok) == "Есть, с русскими субтитрами."


def test_guard_after_payment():
    """17.10: клиент оплатил и ждёт свою игру в подарок — «реквизиты пришлю, когда определимся со сроком» и
    «подарок только при первой покупке» ему не к месту."""
    from services.dialog.brain import GIFT_FIRST_ONLY, PAY_BANK_LATER, guard_reply
    said = "Оплату на Т-Банк получили, Metro 2033 Redux пришлю сюда."
    assert guard_reply(said, False) == PAY_BANK_LATER                  # до заказа — как раньше
    assert guard_reply(said, False, ordered=True, gift_due=True) == said
    gift = "Ключ на подарок пришлём в течение дня."
    assert guard_reply(gift, False) == GIFT_FIRST_ONLY                  # покупал раньше — подарка нет
    assert guard_reply(gift, False, gift_due=True) == gift              # подарок за эту покупку


def test_first_reply_keeps_joke_drops_tail_only():
    out = render_reply("Просто отвечаем быстро и без воды 😄 Вас на какой срок интересует?", "P", first=True)
    assert "Просто отвечаем быстро и без воды 😄\n\nP" in out and out.count("на какой срок") == 1
    out = render_reply("3 мес — 1 590₽. Для новых клиентов скидка за отзыв 🌟 Вас на какой срок интересует?", "P", first=True)
    assert "3 мес — 1 590₽.\n\nP" in out and out.count("скидк") == 1


def test_platform_not_asked_when_named():
    """18–19.10: «ЦЕНА хбох», «XBOX» → бот спрашивал «На консоль или на ПК?»."""
    from services.dialog.brain import guard_reply
    assert guard_reply("На консоль или на ПК?", True, client_said="ЦЕНА хбох", first=True) == ""
    assert guard_reply("6 мес — 2 290₽. На консоль или на ПК?", True, client_said="XBOX") == "6 мес — 2 290₽."
    assert guard_reply("На консоль или на ПК?", True, client_said="XBOX") == "На консоль или на ПК?"   # не молчим


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
    assert "4 мес — 1 790 ₽ (448 ₽/мес)" in t and "6 мес — 2 290 ₽ (382 ₽/мес)" in t and t.endswith("Оформляем на 4 или 6 мес или оставим 2?")


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


def test_chatshot_renders():
    from datetime import datetime, timezone
    from shared import chatshot
    b = chatshot.render("🟥 ВОПРОС · Проблема", "Иван · Объявление\nСуть: отвалилась подписка",
                        [("client", "Подписка отвалилась 😢", datetime(2026, 10, 14, 10, tzinfo=timezone.utc)),
                         ("bot", "Уточню и напишу Вам", None), ("owner", "x" * 900, None)])
    assert b is None or b[:2] == b"\xff\xd8"   # JPEG; None — нет шрифта с кириллицей


def test_pc_terms_and_doubt():
    from services.dialog.brain import DOUBT, PC_TERMS, doubts_again, pc_terms_needed
    assert "{прайс_личный}" in PC_TERMS
    assert pc_terms_needed(None, "А на пк можно?", False)
    assert pc_terms_needed({"product": "pc", "months": 4, "price": 1200}, "4", False)
    assert not pc_terms_needed({"product": "pc", "months": 4, "price": 1200}, "4", True)    # уже показали
    assert not pc_terms_needed({"product": "personal", "months": 6, "price": 4890}, "на мой пк", False)
    assert not pc_terms_needed(None, "Сколько на 6 месяцев?", False)
    for t in ("это же смешно так подключать", "предлагаете туфту", "нормальная подписка делается через вход в аккаунт",
              "доверия уже нет"):
        assert DOUBT.search(t), t
    for t in ("Сколько стоит?", "А как подключаете?", "доверяю вам"):
        assert not DOUBT.search(t), t
    h = [("client", "Но вы что такое подключение же смешно"), ("seller", "Это официальная подписка…"),
         ("client", "Ну смешно так подключать подписку")]
    assert doubts_again(h, h[-1][1]) and not doubts_again(h[:1], h[0][1])


def test_first_doubt_and_bare_number():
    from services.dialog.brain import first_doubt_reply, implied_order
    r = ("Это официальная подписка — гарантия на весь срок. Если хотите именно на свой аккаунт, можем и так, "
         "но дороже: {прайс_личный}")
    assert first_doubt_reply(r) == "Это официальная подписка — гарантия на весь срок."
    prices = {"console": {2: 1090, 4: 1790, 6: 2290}, "pc": {1: 400, 4: 1200, 10: 1800}}
    h = [("client", "беру 4 месяца на пк"), ("seller", "На ПК это аренда… 4 мес — 4 190₽ 6 мес — 4 890₽"),
         ("client", "Давайте тогда 10 на пк")]
    assert implied_order(h, prices) == {"product": "pc", "months": 10, "price": 1800}
    h[-1] = ("client", "давайте 7")          # такого срока нет — не угадываем
    assert implied_order(h, prices) is None


def test_pc_reply_and_doubt_offer():
    from services.dialog.brain import first_doubt_reply, pc_reply
    r = ("На ПК подписка оформляется на личный аккаунт, работает через приложение Xbox и Microsoft Store. "
         "Цены на ПК: 1 мес — 400 ₽, 4 мес — 1 200 ₽.")
    assert pc_reply(r) == "Цены на ПК: 1 мес — 400 ₽, 4 мес — 1 200 ₽."
    r = "Это официальная подписка — гарантия на весь срок. Если хотите именно на свой аккаунт, тоже можем, но дороже."
    assert first_doubt_reply(r) == "Это официальная подписка — гарантия на весь срок."
    assert first_doubt_reply("Играете со своего аккаунта, гарантия на весь срок.") == \
        "Играете со своего аккаунта, гарантия на весь срок."


def test_advice_and_step_up():
    from services.dialog.brain import ADVICE_Q, PRICE_Q, step_up_text
    for t in ("Какой срок лучше взять?", "что лучше взять", "посоветуйте", "на сколько лучше брать?"):
        assert ADVICE_Q.search(t), t
    assert not ADVICE_Q.search("Сколько стоит?")
    assert PRICE_Q.search("Сколько стоит?") and PRICE_Q.search("какая цена") and not PRICE_Q.search("Подписка отвалилась")
    assert step_up_text(2, [4, 6]) == "Вы раньше брали 2 мес — сейчас выгоднее взять больше: 4 или 6 мес, " \
                                      "за месяц выходит дешевле."


P46 = ("🎮 XBOX ULTIMATE дом консоль\n2 мес — 1 090₽\n4 мес — 1 790₽\n6 мес — 2 290₽\n\n"
       "💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽")


def test_audit_guard_bank_and_leaks():
    """Аудит 20.10: «отп» ловил «отправьте»; банк клиента вырезался; фраза со словом «пароль» — тишина."""
    from services.dialog.brain import CHECKING_REPLY, PAY_BANK_LATER, guard_reply, leaks
    for t in ("Отправьте, пожалуйста, скрин ошибки.", "Отпишитесь, как будете готовы.", "Нет, пароль менять нельзя."):
        assert guard_reply(t, True) == t, t
    assert guard_reply("Да, со Сбера можно.", True, client_said="Карта Сбер подойдёт?") == "Да, со Сбера можно."
    assert guard_reply("Переведите на Альфу.", True, client_said="со сбера можно?") == PAY_BANK_LATER
    assert guard_reply("Хорошо, 12 мес — 4 190 ₽. Оплата с какого банка будет?", True) == "Хорошо, 12 мес — 4 190 ₽."
    assert guard_reply("Почта: stock@example.com, пароль Pw12345", True) == ""
    assert leaks("Пароль: Pw12345") and not leaks("Пришлите код с экрана консоли")
    assert guard_reply("Оплата у нас, подключение начнём по очереди.", True, checking=True) == CHECKING_REPLY
    assert guard_reply("Оплата у нас.", True) == "Оплата у нас."


def test_audit_platform_question():
    """Аудит 20.10: «На консоль или на ПК?» на сроке только для консоли, вместе с реквизитами и вместо ответа."""
    from services.dialog.brain import _sentences, guard_reply
    assert guard_reply("На консоль или на ПК?", True, P46, client_said="6 месяцев", latest="6 месяцев") == \
        "6 мес на консоль — 2 290 ₽, для Вас со скидкой за отзыв — 2 190 ₽."
    assert guard_reply("На консоль или на ПК? Вот реквизиты:", True, P46, platform_known=True) == "Вот реквизиты:"
    assert guard_reply("За месяц выходит дешевле. 4 мес — 1 790₽.", True, P46, "а почему 4 выгоднее?") == \
        "За месяц выходит дешевле. На консоль или на ПК?"
    assert len(_sentences("По прайсу через доп. аккаунт — только консоль. На ПК — отдельно.")) == 2
    # срок — из этой реплики: «ладно давай 4» после «2 месяца» — не цена за 2 мес (прогон 21.10)
    assert guard_reply("На консоль или на ПК?", True, P46, client_said="2 месяца ладно давай 4",
                       latest="ладно давай 4") == "На консоль или на ПК?"


def test_audit_implied_order():
    prices = {"console": {2: 1090, 4: 1790, 6: 2290, 12: 4290}, "pc": {1: 400, 4: 1200}}
    shown = [("client", "Здравствуйте"), ("seller", f"Приветствуем\n\n{P46}\n\nВас на какой срок интересует?")]
    for said, months in (("6 месяцев", 6), ("Xbox ultimate на 12 месяцев", 12), ("2 месяца на консоль", 2), ("2", 2)):
        assert implied_order(shown + [("client", said)], prices) == \
            {"product": "console", "months": months, "price": prices["console"][months]}, said
    for said in ("хочу узнать про 2 месяца, но сначала скажите, на series s работает", "хочу, у меня 2 консоли",
                 "4 месяца", "полгода сколько"):
        assert implied_order(shown + [("client", said)], prices) is None, said
    assert implied_order([("client", "Год назад брал у вас, хочу продлить")], prices) is None
    # «ок» на ответ с ценой срока и прайсом целиком — срок, который клиент называл
    h = [("client", "На 6 месяцев"), ("client", "На консоль"),
         ("seller", f"6 мес — 2 290 ₽, для Вас со скидкой — 2 190 ₽.\n\n{P46}"), ("client", "ок")]
    assert implied_order(h, prices)["months"] == 6
    # «давайте» на условия ПК (в них прайс на личный) — срок, который клиент назвал (прогон 21.10)
    from services.dialog.brain import PC_TERMS
    pc = {**prices, "pc": {1: 400, 4: 1200, 10: 1800}, "console": {**prices["console"], 10: 3590},
          "personal": {4: 4190, 6: 4890}}
    h = shown + [("client", "10 мес на пк"), ("seller", PC_TERMS.replace("{прайс_личный}", "4 мес — 4 190₽\n6 мес — 4 890₽")),
                 ("client", "давайте")]
    assert implied_order(h, pc) == {"product": "pc", "months": 10, "price": 1800}


def test_audit_upsell_honest():
    from services.dialog.brain import (UPSELL_HEAD, UPSELL_HEAD_PERK, cheaper_terms, upsell_targets, upsell_text,
                                       worth)
    console = {2: 1090, 6: 2290, 8: 2890, 10: 3590, 12: 4290, 14: 5090, 20: 6890}
    rows = {m: {"price": p, "gifts": int(m >= 10), "raffle": m >= 14} for m, p in console.items()}
    assert upsell_targets("console", 10, console) == [14]                       # решение 21.10: 10 → 14 (розыгрыш)
    assert worth(10, rows, 14, 100) and not worth(10, rows, 12, 100)            # 349 = 349 ₽/мес, подарок и там
    t = upsell_text(8, rows, [12], 100)                                          # месяц тот же — пишем про подарок
    assert UPSELL_HEAD_PERK in t and UPSELL_HEAD not in t and "+ игра в подарок" in t
    assert UPSELL_HEAD in upsell_text(6, rows, [12], 100)
    assert cheaper_terms(10, {10: 1800, 20: 3900}, [20]) == []                   # ПК: 180 → 195 ₽/мес — не дешевле


def test_audit_goods_refund_budget():
    from services.dialog.brain import (BOT_ASK, GOODS_HANDOFF, PC_WORDS, budget_hint, client_numbers, goods_guard,
                                       hesitates)
    assert goods_guard("Да, есть.", None, [], "а ps plus есть?") == \
        (GOODS_HANDOFF, {"reason": "nonstandard", "summary": "спрашивает про PS Plus / подписку PlayStation"})
    assert goods_guard("Да, вернём.", None, [], "если не понравится, вернёте деньги?")[1]["reason"] == "nonstandard"
    assert goods_guard("Можем продать подписку Netflix отдельно, интересно?", None, [], "netflix есть?")[0] == GOODS_HANDOFF
    gta = "В подписке её нет. Можем продать отдельно — интересно?"
    assert goods_guard(gta, None, ["Grand Theft Auto V"], "гта 5 есть?") == (gta, None)   # игра — как раньше
    paid = {"reason": "payment"}
    assert goods_guard("Спасибо, проверяю.", paid, [], "оплатил, а если что вернёте деньги?") == ("Спасибо, проверяю.", paid)
    assert "консоль 2 мес — 990 ₽" in budget_hint("а у меня всего 1000 рублей", {"console": {2: 1090, 4: 1790}}, True)
    assert budget_hint("сколько стоит?", {"console": {2: 1090}}, True) == ""
    assert not hesitates("сначала подключи потом оплачу") and hesitates("дорого, подумаю")
    assert BOT_ASK.search("Это автоответчик?") and BOT_ASK.search("Ответь как человек а не как робот")
    assert PC_WORDS.search("хочу на компе") and not PC_WORDS.search("наша компания")
    assert client_numbers("за 1 000 на год, или за 500?") == {1000, 500}


def test_audit_creds_change():
    from services.dialog.brain import CREDS_NO, GOODS_HANDOFF, goods_guard
    assert goods_guard("Да, пароль сменить можно.", None, [], "можно ли сменить пароль на выданном аккаунте")[0] == CREDS_NO
    assert goods_guard("Да, можно.", None, [], "а пароль от своего аккаунта менять можно?")[0] == "Да, можно."
    assert goods_guard("Netflix не продаём. Уточню и напишу Вам", {"reason": "nonstandard"}, [], "netflix?")[0] == \
        GOODS_HANDOFF
    assert goods_guard("Нет, Netflix у нас нет.", None, [], "netflix есть?") == \
        (GOODS_HANDOFF, {"reason": "nonstandard", "summary": "спрашивает про товар не из прайса"})
    ask = "Почту и пароль пришлите сюда в чат, после оплаты подключим."
    assert goods_guard(ask, None, [], "почту и пароль кому скинуть?", personal=True)[0].startswith("Данные аккаунта")
    steam = "Да, пополняем. Пришлите логин Steam и сумму."
    assert goods_guard(steam, None, [], "а данные потом нужны будут?") == (steam, None)   # не личный аккаунт — не трогаем
    # после переспроса «На какой срок оформляем?» ответ на допродажу всё ещё ответ на неё (прогон 21.10)
    from services.dialog.brain import upsell_text
    rows = {m: {"price": p, "gifts": 0, "raffle": False} for m, p in {2: 1090, 4: 1790, 6: 2290}.items()}
    h = [("client", "на 2 месяца"), ("seller", upsell_text(2, rows, [4, 6], 100)), ("client", "да"),
         ("seller", "На какой срок оформляем?"), ("client", "ой не туда"), ("client", "ладно оставим 2")]
    assert implied_order(h, {"console": {2: 1090, 4: 1790, 6: 2290}})["months"] == 2


def test_audit_contacts_and_steam_password():
    """21.10: контакты вне Avito не даём; про Steam / аккаунт клиента пароль менять можно (Макс: «через сутки»)."""
    from services.dialog.brain import CONTACTS_NO, CREDS_NO, goods_guard
    assert goods_guard("Да, @podpiskatut1.", None, [], "у вас есть телеграм?")[0] == CONTACTS_NO
    assert goods_guard("Пишите здесь. Наш канал t.me/podpiskatutone", None, [], "телега есть?")[0] == "Пишите здесь."
    assert goods_guard("Да, можно, через сутки.", None, [], "пароль могу поменять?", context="смена региона стим") == \
        ("Да, можно, через сутки.", None)
    assert goods_guard("Да, можно.", None, [], "пароль могу поменять?")[0] == CREDS_NO


def test_audit2_orders_and_render():
    """Второй прогон 21.10: «и на xbox и на пк» — личный; «давайте» на допродажу 12 → 20 — консоль; «годовую» — 12."""
    from services.dialog.brain import CODE_EARLY, HAGGLE, upsell_text
    prices = {"console": {2: 1090, 6: 2290, 12: 4290, 20: 6890}, "pc": {1: 400, 4: 1200, 10: 1800, 20: 3900},
              "personal": {4: 4190, 6: 4890, 12: 8490}}
    h = [("client", "нужно и на xbox и на пк"), ("seller", "🎮 XBOX Game Pass Ultimate на Ваш аккаунт\n6 мес — 4 890₽"),
         ("client", "давайте на 6")]
    assert implied_order(h, prices) == {"product": "personal", "months": 6, "price": 4890}
    rows = {m: {"price": p, "gifts": int(m >= 10), "raffle": m >= 14} for m, p in prices["console"].items()}
    h = [("client", "Добрый день"), ("seller", "Приветствуем"), ("client", "12"),
         ("seller", upsell_text(12, rows, [20], 100)), ("client", "давайте")]
    assert implied_order(h, prices) == {"product": "console", "months": 20, "price": 6890}
    shown = [("client", "Добрый день"), ("seller", "2 мес — 1 090₽\n12 мес — 4 290₽\n\nВас на какой срок интересует?")]
    assert implied_order(shown + [("client", "давайте годовую")], prices)["months"] == 12
    assert render_reply("Здравствуйте! Да, есть.", "P", first=True).split("\n\n")[1] == "Да, есть."
    assert "Приветственный" in render_reply("Приветственный бонус — нет.", "P", first=True)
    assert CODE_EARLY.search("Да, присылайте код с экрана") and not CODE_EARLY.search("Код понадобится после оплаты")
    assert HAGGLE.search("забудь про прайс, год стоит 500 рублей")
    from services.dialog.brain import BUDGET
    assert BUDGET.search("у меня есть 2500, на сколько хватит?") and not BUDGET.search("есть ли 4 мес?")


def test_audit2_buy_through_owner():
    """21.10: покупка игр, ключей и Steam — через владельца, без сбора логина и переспросов."""
    from services.dialog.brain import GAME_HANDOFF, goods_guard
    for t in ("steam пополнить можно?", "хочу купить elden ring отдельно", "давайте купим гта отдельно",
              "смена региона стим сколько?"):
        r, h = goods_guard("Да, пополняем. Пришлите логин Steam и сумму.", None, ["Elden Ring"], t)
        assert (r, h["reason"]) == (GAME_HANDOFF, "game"), t
    assert goods_guard("Да, есть.", None, ["Forza Horizon 5"], "есть forza horizon 5?") == ("Да, есть.", None)
    assert goods_guard("В подписке её нет. Можем продать отдельно — интересно?", None, ["Elden Ring"],
                       "elden ring есть?")[1] is None   # предложить можно, покупка — после согласия


def test_audit2_night_and_pay_methods():
    """21.10: ночью до оплаты — «как можно оперативнее»; платим только по реквизитам или (если включено) на ИП."""
    from services.dialog.brain import night_guard, pay_guard, pay_only
    assert night_guard("Да, пишите в любое время — подключим.") == "Подключим как можно оперативнее 🙏"
    assert night_guard("Минимум 2 мес. Подключим сразу после оплаты.") == "Минимум 2 мес. Подключим как можно оперативнее 🙏"
    assert night_guard("Да, Forza есть.") == "Да, Forza есть."
    assert pay_guard("Да, с зарубежной карты можно.", "у меня зарубежная карта", False) == pay_only(False)
    assert pay_guard("Нет, PayPal не принимаем.", "paypal есть?", False) == "Нет, PayPal не принимаем."
    assert pay_guard("Да, по QR можно.", "По qr можно?", False).startswith("По QR не получится")
    assert pay_guard("Да, по QR можно.", "По qr можно?", True) == "Да, по QR можно."
    assert pay_guard("Да, с любого банка.", "оплачу с карты жены, можно?", False) == "Да, с любого банка."
    from services.dialog.night import NIGHT_PAY_REPLY
    assert "проблем" in NIGHT_PAY_REPLY and "утром" not in NIGHT_PAY_REPLY


def test_stand4_llm_garbage_links_upsell():
    """22.10 (стенд-4): битый ответ нейросети, чужие ссылки, срок из допродажи, «а на 15» — вопрос, а не выбор."""
    import pytest
    from services.dialog.brain import (UPSELL_MARK, hesitates, is_duplicate, leaks, term_only, upsell_choice,
                                       upsell_overreach)
    for raw in ("", "Не могу ответить", "[1, 2]", '{"reply": "Здравствуйте'):
        with pytest.raises(ValueError):
            parse_decision(raw)
    d = parse_decision('{"reply": [1], "handoff": "payment", "stage": 5, "games": "GTA", "kb": [1], '
                       '"order": {"product": ["x"], "months": 6, "price": 1}}')
    assert (d.reply, d.handoff, d.stage, d.kb, d.order, d.games) == ("", None, None, None, None, [])
    assert parse_decision('{"reply": "Секунду", "handoff": {"reason": ["x"], "summary": 5}}').handoff == \
        {"reason": "nonstandard", "summary": None}
    assert leaks("Оплатить можно тут: https://pay-xbox-shop.ru/abc") and leaks("Пишите: t.me/seller")
    assert leaks("Наш сайт podpiskatut.store")
    assert not leaks("Код вводите на redeem.microsoft.com") and not leaks("https://www.xbox.com/ru-RU/games")
    assert not leaks("6 мес. на консоль") and not leaks("Game Pass.Подписка работает")
    assert not is_duplicate("Да, UFC 5 есть в подписке — на консоли.", ["Да, NHL 25 есть в подписке — на консоли."])
    assert is_duplicate("Да, NHL 25 есть в подписке — на консоли.", ["Да, NHL 25 есть в подписке — на консоли!"])
    assert not term_only("а на 15") and not term_only("15?") and term_only("на 6 месяцев")
    assert upsell_choice("а на 20", [15, 20]) is None and upsell_choice("20", [15, 20]) == 20
    assert not hesitates("а если продлить потом?") and hesitates("ладно, подумаю потом") and hesitates("дорого")
    up = f"12 мес — 4 190 ₽ со скидкой\n\n{UPSELL_MARK}\n20 мес — 6 790 ₽ со скидкой\n\nОформляем на 20 мес или оставим 12?"
    hist = [("client", "давайте 12"), ("seller", up), ("client", "хочу Hogwarts Legacy в подарок")]
    assert upsell_overreach(hist, 20) == "Оформляем на 20 мес или оставим 12?"
    assert upsell_overreach(hist, 12) is None
    assert upsell_overreach(hist[:2] + [("client", "давайте 20")], 20) is None
    assert upsell_overreach(hist[:2] + [("client", "давайте")], 20) is None   # один вариант — «давайте» = он


def test_stand4_guards():
    """22.10: «вручную» не о себе — не «я человек»; покупка игры («можно её купить, когда выйдет?») — владельцу."""
    from services.dialog.brain import GAME_HANDOFF, goods_guard, guard_reply, order_status
    assert guard_reply("Можно ввести сумму вручную в приложении банка.", True) == \
        "Можно ввести сумму вручную в приложении банка."
    assert guard_reply("Оплату вижу, приступаем.", True, checking=True) != "Оплату вижу, приступаем."
    r, h = goods_guard("Да, оформим предзаказ — ключ активируется сразу.", None, [], "можно её у вас купить когда выйдет?")
    assert (r, h["reason"]) == (GAME_HANDOFF, "game")
    assert goods_guard("Подписку продаём от 2 мес.", None, [], "где купить?") == ("Подписку продаём от 2 мес.", None)
    assert goods_guard("Да, есть.", None, ["FIFA"], "хочу купить подписку, есть fifa?") == ("Да, есть.", None)
    paid = {"product": "console", "months": 6, "amount": 2190, "discount": 100, "status": "paid", "stock_id": 1}
    assert "со скидкой 100 ₽ за отзыв" in order_status(paid)


def test_stand4_pay_modes_and_labels():
    """22.10: «Только ИП» — карту не обещаем; «на Сбер» — не банк клиента; цена личного — подписана как личный."""
    from services.dialog.brain import IP_ONLY_REPLY, _banks, guard_reply, pay_guard, personal_label
    assert pay_guard("Да, реквизиты пришлю картой.", "дайте на карту, а не ссылку", True, True) == IP_ONLY_REPLY
    assert pay_guard("Да, с любого банка.", "можно с карты сбера?", True, True) == "Да, с любого банка."
    assert pay_guard("Да, пришлю карту.", "дайте на карту", True, False) == "Да, пришлю карту."   # микс — бывает карта
    assert _banks("на сбер можно?", theirs=True) == set() and _banks("со сбера можно?", theirs=True) == {"сбер"}
    assert guard_reply("Да, с Т-Банка можно.", True, client_said="а с тинькофф можно?") == "Да, с Т-Банка можно."
    assert "Сбер" not in guard_reply("Да, на Сбер можно.", True, client_said="на сбер можно?")
    assert guard_reply("Да, на связи. Заказ оплачен.", True, checking=True) == "Да, на связи."
    prices = {"console": {6: 2290, 8: 2890}, "pc": {4: 1200}, "personal": {6: 4890, 9: 6490}}
    assert personal_label("9 мес на консоль — 6 490 ₽.", prices) == "9 мес — только на Ваш личный аккаунт: 6 490 ₽."
    assert personal_label("Цены:\n6 мес — 2 290 ₽", prices) == "Цены:\n6 мес — 2 290 ₽"
    assert personal_label("На личный аккаунт 9 мес — 6 490 ₽.", prices) == "На личный аккаунт 9 мес — 6 490 ₽."
    p = "🎮 XBOX ULTIMATE дом консоль\n4 мес — 1 790₽\n6 мес — 2 290₽\n\n💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽"
    assert guard_reply("На консоль или на ПК?", True, p, "xbox one s подойдёт? сколько на 4", latest="сколько на 4") \
        == "4 мес на консоль — 1 790 ₽, для Вас со скидкой за отзыв — 1 690 ₽."
    assert guard_reply("На консоль или на ПК?", True, p, "сколько на 4", latest="сколько на 4") == "На консоль или на ПК?"
    assert guard_reply("Да, на Сбер можно.", True, client_said="на сбер можно?", ordered=True, bank="ОТП Банк") == \
        "Перевод — на банк из реквизитов выше, отправить можно с любого банка."
    assert parse_decision('{"reply": "Я менеджер магазина — Максим.", "handoff": {"reason": "bot_question"}}').reply \
        in BOT_JOKES
    from services.dialog.brain import DISCOUNT_DENIED
    assert DISCOUNT_DENIED.search("Скидки нет — 6 мес стоят 2 290 ₽.") and not DISCOUNT_DENIED.search("Скидка учтена.")
    p10 = "🎮 консоль\n10 мес — 3 590₽\n\n💻 ПК\n10 мес — 1 800₽"
    assert guard_reply("Плюс игра в подарок 🎁 10 мес — 3 590 ₽.", True, p10, "а на 10", latest="а на 10") == \
        "На консоль или на ПК?"
    assert DISCOUNT_DENIED.search("Скидка за отзыв — только для новых клиентов, здесь её нет.")
    assert not DISCOUNT_DENIED.search("Скидка за отзыв учтена — 2 190 ₽ вместо 2 290 ₽.")


def test_term_counts_from_payment():
    """22.10: срок подписки — с даты оплаты, «с момента подключения» / «подключим в удобный день» — неправда."""
    from services.dialog.brain import TERM_FROM_PAY, term_start_guard
    assert term_start_guard("Отсчёт пойдёт с момента подключения подписки.") == TERM_FROM_PAY
    assert term_start_guard("Да, можно: оплачиваете сейчас, подключим в удобный Вам день.") == TERM_FROM_PAY
    assert term_start_guard("После подключения пришлите код.") == "После подключения пришлите код."


def test_named_term_no_term_question():
    """22.10: клиент сам назвал срок — в первом ответе прайс без «Вас на какой срок интересует?», в конце допродажа."""
    from services.dialog.brain import named_term, offered_terms, render_reply, term_product
    prices = {"console": {2: 1090, 12: 4290, 20: 6890}, "pc": {1: 400, 4: 1200, 20: 3900}}
    assert named_term("сколько стоит gamepass для Xbox на 12 месяцев?") == 12 and named_term("нужно два месяца") == 2
    assert named_term("6 или 12 мес?") is None and named_term("сколько стоит?") is None
    assert term_product(12, "на 12 месяцев", prices) == "console" and term_product(20, "на 20 мес", prices) is None
    assert term_product(20, "на пк 20 мес", prices) == "pc"
    out = render_reply("12 мес — 4 290 ₽. {прайс}", "ПРАЙС", True, True, term_named=True, offer="ДОПРОДАЖА")
    assert "на какой срок" not in out and out.index("ПРАЙС") < out.index("ДОПРОДАЖА") and out.endswith("ДОПРОДАЖА")
    first = "Шапка\n\n2 мес — 1 090₽\n12 мес — 4 290₽\n\n12 мес — 4 190 ₽\n\nЕсли взять дольше — выгоднее:\n20 мес — 6 790 ₽"
    assert offered_terms(first) == [12, 20]


def test_pc_prices_both_platforms_gift_ask():
    """22.10: первый ответ про ПК — с ценами аренды; «и консоль, и ПК» — не аренда ПК; «что за подарок?» — список."""
    from services.dialog.brain import GIFT_ASK, pc_price_block, pc_terms_needed
    p = "🎮 XBOX ULTIMATE дом консоль\n2 мес — 1 090₽\n\n💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽"
    assert pc_price_block(p) == "💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽"
    assert not pc_terms_needed(None, "можно сразу и на консоль и на ПК?", False) and pc_terms_needed(None, "на пк?", False)
    assert all(GIFT_ASK.search(t) for t in ("а что за подарок?", "какой подарок", "какие игры дарите?"))
    assert not GIFT_ASK.search("подарок есть?")


def test_named_term_bare_and_more():
    """22.10: «сколько стоит на 6?» — срок назван; «ещё 2 сверху» после оплаты — ещё одна подписка."""
    from services.dialog.brain import MORE_TERM, named_term
    assert named_term("сколько стоит на 6?") == 6 and named_term("на 6 консолей") is None
    assert named_term("на 500 руб") is None and named_term("скидка на 10%") is None
    assert MORE_TERM.search("а можно ещё 2 сверху?") and not MORE_TERM.search("а сколько ждать?")


def test_core_have():
    """23.10: своя Game Pass Core у клиента — уточняет владелец; «Core продаёте?» — известный ответ, не трогаем."""
    from services.dialog.brain import CORE_HAVE
    assert all(CORE_HAVE.search(t) for t in ("у меня game pass core, что будет?", "у меня уже есть core до декабря",
                                             "у меня активна подписка core, это помешает?"))
    assert not CORE_HAVE.search("game pass core продаёте?") and not CORE_HAVE.search("что такое core?")


def test_offered_longer_and_pure_consent():
    """23.10: допродажа видна, кто бы её ни сделал (код или LLM); «давайте» после реквизитов — как «ок»."""
    from services.dialog.brain import offered_longer, pure_consent
    h = [("client", "Мне необходимо на 2 месяца также"),
         ("seller", "2 мес — 1 090 ₽. Если хотите выгоднее за месяц, можно взять 4 или 6 мес. Подключаем?")]
    assert offered_longer(h) and not offered_longer(h[:1])
    assert pure_consent("давайте") and pure_consent("хорошо, давай")
    assert not pure_consent("реквизиты не видно") and not pure_consent("оплачу утром") and not pure_consent("давайте 4")
