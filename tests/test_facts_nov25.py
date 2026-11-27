"""25.11 (PREMIUM, «гта в подарок будет на 10 мес?»): список подарков нейросеть видела только после оплаты, код вырезал
её «да, подарок будет» (платформа не названа), на «?» она ответила «Да, на 10 мес GTA 6 будет в подарок» — а GTA 6
только в розыгрыше от 14 мес. Леонид: «бот должен знать всю инфу постоянно, а не перед продажей одно, после другое»."""
from datetime import datetime, timezone

from services.dialog import brain, lessons
from shared import catalog

PRICE = ("🎮 XBOX ULTIMATE дом консоль (играете со своего аккаунта)\n2 мес — 1 090₽\n4 мес — 1 790₽\n6 мес — 2 290₽\n"
         "10 мес — 3 590₽ 🎁\n12 мес — 4 290₽ 🎁\n14 мес — 5 090₽ 🎁 + розыгрыш\n20 мес — 6 890₽ 🎁 + розыгрыш\n"
         "🎁 Игра в подарок от 10 мес; от 14 мес — ещё и участие в розыгрыше: GTA 6, EA Sports FC 27\n\n"
         "💻 ПК\n1 мес — 400₽\n4 мес — 1 200₽\n10 мес — 1 800₽\n20 мес — 3 900₽")
GIFT_LIST = ("🎇 Выберите из списка игру в подарок🎇\n\n💎OUTLAST: BUNDLE OF TERROR\n💎Terraria\n💎 Metro 2033 Redux\n\n"
             "🔸 Игры активируются на ваш аккаунт и остаются навсегда.\n🔸 Выдача  в течении 5–10 минут.")
PROMO = {"gift_terms": [10, 12, 13, 14, 15, 20], "raffle_terms": [14, 15, 20],
         "gift_games": catalog.list_items(GIFT_LIST), "raffle_games": ["GTA 6", "EA Sports FC 27"]}


def test_gift_list_items():
    assert catalog.list_items(GIFT_LIST) == ["OUTLAST: BUNDLE OF TERROR", "Terraria", "Metro 2033 Redux"]
    assert catalog.list_items("GTA 6\nEA Sports FC 27\n") == ["GTA 6", "EA Sports FC 27"]


def test_facts_new_client():
    f = brain.facts_block(PROMO, new=True, purchases=[], issue="creds", pay="ip")
    assert "Terraria" in f and "Metro 2033 Redux" in f and "Выберите" not in f and "🔸" not in f
    assert "10, 12, 13, 14, 15, 20 мес" in f and "GTA 6" in f and "не дарим" in f
    assert "новый — подарок положен" in f and "Покупок у нас ещё не было" in f
    assert "логин и пароль" in f and "только по ссылке СБП" in f


def test_facts_any_stage_and_status():
    paid = datetime(2026, 10, 1, tzinfo=timezone.utc)
    f = brain.facts_block(PROMO, new=False, purchases=[{"product": "console", "months": 6, "paid_at": paid,
                                                        "expires_at": datetime(2027, 4, 1, tzinfo=timezone.utc)}],
                          now=datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert "уже покупал — подарка и розыгрыша нет" in f
    assert "6 мес, оплачена 01.10.2026, до 01.04.2027 (действует)" in f
    assert "Terraria" in f   # список виден и покупавшему: на «Terraria в подарок?» он ответит по фактам
    assert "уже покупал" not in brain.facts_block(PROMO, new=False, gift_due=True)
    assert "на срок без подарка" in brain.facts_block(PROMO, new=False, gift_short=True)


def test_facts_no_raffle():
    f = brain.facts_block({**PROMO, "raffle_terms": []}, new=True)
    assert "Розыгрыша сейчас нет" in f and "участие в розыгрыше" not in f


def test_facts_in_user_prompt():
    facts = brain.facts_block(PROMO, new=True)
    assert "ФАКТЫ" in brain.user_prompt(PRICE, [("client", "гта в подарок будет?")], facts=facts)


def test_store_facts_always_in_system():
    s = brain.system_prompt("база")
    assert "ПОСТОЯННЫЕ ОТВЕТЫ" in s and "12+1" in s and "аренда нашего аккаунта" in s


def test_gift_answer_kept_when_asked():
    reply = "Да, на 10 мес игра в подарок будет. 10 мес — 3 590 ₽, для Вас со скидкой за отзыв — 3 490 ₽."
    said = "Добрый день если беру подписку на 10 месяцев гта в подарок будет?"
    why = []
    out = brain.guard_reply(reply, True, PRICE, said, latest=said, first=True, why=why)
    assert "подарок" in out and "На консоль или на ПК?" in out and "₽" not in out
    assert why and "консоли" in why[0]
    # о подарке не спрашивали — как раньше: без цены «игра в подарок 🎁» висит обрывком
    out = brain.guard_reply("10 мес — 3 590 ₽ + игра в подарок.", True, PRICE, "сколько на 10?", latest="сколько на 10?")
    assert out == "На консоль или на ПК?"


def test_cut_note():
    why = []
    out = brain.guard_reply("Подарок за эту покупку будет. Оформляем?", False, PRICE, "а скидка?", latest="а скидка?",
                            why=why)
    note = brain.cut_note("Подарок за эту покупку будет. Оформляем?", out, why)
    assert "«Подарок за эту покупку будет.»" in note and "уже покупал" in note and "Оформляем" not in note.split("Причина")[0]
    assert brain.cut_note("Оформляем?", "Оформляем?", []) == ""


def test_no_rule_limit():
    assert not hasattr(lessons, "MAX_RULES")


def test_understanding_parse():
    raw = ('{"question": "будет ли GTA 6 в подарок на 10 мес", "situations": ["gift_list", "вымысел", 5], '
           '"rules": ["П3", 7, "П99", "П3"], "facts": "список подарков", "games": ["GTA 6", ""]}')
    u = brain.parse_understanding(raw, 10)
    assert u["situations"] == {"gift_list"} and u["rules"] == [3, 7] and u["games"] == ["GTA 6"]
    assert brain.has(u, "gift_list", False) and not brain.has(u, "crypto", True)   # разбор есть — шаблон не решает
    assert brain.has(None, "crypto", True) and not brain.has(None, "crypto", None)   # разбора нет — по шаблону
    assert brain.parse_understanding('{"situations": "gift_list"}', 5)["situations"] == set()
    import pytest
    with pytest.raises(ValueError):
        brain.parse_understanding("[]", 5)


def test_understood_hint_and_picked_rules():
    rules = ["Не спорь с клиентом", "Если спрашивают про подарок — сверь со списком"]
    picked = lessons.picked_block(rules, [2, 9])
    assert picked == "П2. Если спрашивают про подарок — сверь со списком"
    u = {"question": "гта в подарок?", "facts": "список подарков", "situations": set(), "rules": [2], "games": []}
    h = brain.understood_hint(u, picked)
    assert "суть — «гта в подарок?»" in h and "П2." in h and "список подарков" in h
    assert brain.understood_hint(None) == ""


def test_goods_guard_by_understanding():
    u = {"question": "", "facts": "", "situations": {"ps_plus"}, "rules": [], "games": []}
    reply, handoff = brain.goods_guard("Да, есть.", None, [], "а плойку подписку делаете?", understood=u)
    assert handoff and handoff["reason"] == "nonstandard"
    none = {**u, "situations": set()}   # разбор: не про PS Plus — шаблон «ps plus» не срабатывает
    assert brain.goods_guard("Нет, только Xbox.", None, [], "ps plus есть?", understood=none)[1] is None


def test_understand_note_lists_all_situations():
    for code in brain.SITUATIONS:
        assert f"- {code} — " in brain.UNDERSTAND_NOTE


def test_games_sold_without_prices_or_game_facts():
    """25.11, Леонид: цены и наличие игр бот не называет («он их перед продажей проверяет сам»), об игре — только есть
    ли она в подписке; платформы, даты выхода, издания — нет; оплату игр Макс принимает сам."""
    s = brain.system_prompt("база")
    assert "Цену и наличие игры не называй никогда" in s and "ключом на аккаунт клиента" in s
    assert "Оплату игр принимает владелец сам" in s and "вышла" in s
    assert "«На Xbox этой игры нет»" not in brain.RULES   # о платформах игр не сообщаем


def test_game_info_goes_to_owner():
    u = {"question": "", "facts": "", "situations": {"game_info"}, "rules": [], "games": ["GTA 6"]}
    r, h = brain.goods_guard("Да, GTA 6 выйдет и на One S/X.", None, ["GTA 6"], "писали что гта 6 пойдёт на one x", understood=u)
    assert (r, h["reason"]) == (brain.GOODS_HANDOFF, "game")
    r, h = brain.goods_guard("В подписке её нет. Уточню и напишу Вам.", None, ["GTA 6"], "когда выйдет гта6?", understood=u)
    assert r == "В подписке её нет. Уточню и напишу Вам." and h["reason"] == "game"
    # покупка: «да, продаём ключом на Ваш аккаунт… цену уточню» — остаётся; без «уточню» — готовая фраза
    buy = {**u, "situations": {"buy_game"}}
    said = "Да, продаём — активируем ключом на Ваш аккаунт, игра останется навсегда. Цену уточню и напишу Вам."
    r, h = brain.goods_guard(said, None, ["GTA 6"], "гта 6 отдельно купить можно?", understood=buy)
    assert r == said and h["reason"] == "game"
    assert brain.goods_guard("Да, 7500 ₽.", None, ["GTA 6"], "почём гта", understood=buy)[0] == brain.GAME_HANDOFF
    # есть ли в подписке — отвечает сам
    sub = {**u, "situations": set()}
    assert brain.goods_guard("Да, есть в подписке.", None, ["Forza Horizon 5"], "форза есть?", understood=sub)[1] is None


def test_game_claims_cut_even_with_handoff():
    """Прогон 25.11: нейросеть сама передала владельцу, но написала «GTA 6 ещё не вышла» — утверждение вырезается."""
    u = {"question": "", "facts": "", "situations": {"game_info"}, "rules": [], "games": ["GTA 6"]}
    r, h = brain.goods_guard("В России GTA 6 ещё не вышла — официального релиза нет. В подписке её нет. Уточню цену "
                             "предзаказа и вернусь к Вам.", {"reason": "game", "summary": "x"}, ["GTA 6"],
                             "в россии ещё нет? вроде уже играют", understood=u)
    assert r == "В подписке её нет. Уточню и напишу Вам." and h == {"reason": "game", "summary": "x"}
    assert "GAME_INFO_HINT" in dir(brain) and "Уточню и напишу Вам" in brain.GAME_INFO_HINT


def test_buy_game_keeps_process_answer():
    """Прогон 25.11 (PREMIUM): «Вы мне скидывайте код и я сам устанавливаю?» — объяснение порядка остаётся."""
    buy = {"question": "", "facts": "", "situations": {"buy_game"}, "rules": [], "games": ["GTA 6"]}
    said = "Активируем игру ключом на Ваш аккаунт: после оплаты понадобятся почта и пароль — войдём и активируем сами."
    r, h = brain.goods_guard(said, None, ["GTA 6"], "вы мне код скидываете и я сам ставлю?", understood=buy)
    assert r == f"{said} {brain.GOODS_HANDOFF}" and h["reason"] == "game"
    for bad in ("Пришлите почту и пароль от аккаунта.", "Да, оформим предзаказ.", "Вот реквизиты для оплаты:"):
        r = brain.goods_guard(bad, None, ["GTA 6"], "хочу купить гта 6", understood=buy)[0]
        assert r in (brain.GAME_HANDOFF, brain.GOODS_HANDOFF), bad   # готовая фраза, ответ нейросети не уходит


def test_price_change_not_promised():
    assert "Подешевеет ли что-то" in brain.STORE_FACTS and "Ваучер" not in brain.STORE_FACTS


def test_game_claims_cut_on_purchase_too():
    buy = {"question": "", "facts": "", "situations": {"buy_game"}, "rules": [], "games": ["GTA 6"]}
    r, h = brain.goods_guard("Ключ на GTA 6 Ultimate должен быть доступен сегодня в 00:00 — активирую сразу, как получу. "
                             "Уточню и напишу Вам.", {"reason": "game", "summary": "x"}, ["GTA 6"],
                             "как там активация предзаказа?", understood=buy)
    assert r == "Уточню и напишу Вам." and h["reason"] == "game"


def test_price_drop_no_info():
    """25.11, Леонид: «подешевеет?» — «нет такой информации и мы её не предоставляем», владельцу не передаём."""
    u = {"question": "", "facts": "", "situations": {"price_drop"}, "rules": [], "games": []}
    r, h = brain.price_drop_guard("Цена фиксируется при оплате — дешевле со временем не станет. Сообщить смогу, если "
                                  "появятся изменения.", None, u)
    assert (r, h) == (brain.PRICE_DROP_REPLY, None)
    r, h = brain.price_drop_guard("Уточню и напишу Вам.", {"reason": "nonstandard", "summary": "когда подешевеет"}, u)
    assert (r, h) == (brain.PRICE_DROP_REPLY, None)
    # передача по другой причине (игра) остаётся вместе с «уточню»
    r, h = brain.price_drop_guard("В подписке её нет. Уточню и напишу Вам.", {"reason": "game", "summary": "x"}, u)
    assert r == f"В подписке её нет. {brain.PRICE_DROP_REPLY} Уточню и напишу Вам." and h["reason"] == "game"
    assert brain.price_drop_guard("Скоро подешевеет.", None, None) == ("Скоро подешевеет.", None)   # разбора нет
    assert brain.PRICE_DROP_REPLY in brain.STORE_FACTS and "price_drop" in brain.SITUATIONS
    assert not brain.PRICE_TALK.search("Оплата пройдёт одной транзакцией.")


def test_variant_open_both_answers():
    """20.11: «на мой акк?» → оба варианта → «Сами подключаете?» — ответ про оба, часть про доп. не теряется."""
    u = {"question": "", "facts": "", "situations": {"variant_open"}, "rules": [], "games": []}
    reply = "Через дополнительный аккаунт — добавим его на консоль, нужно быть рядом. На личный — нужны почта и пароль."
    r, _ = brain.goods_guard(reply, None, [], "сами подключаете?", True, understood=u)
    assert r.startswith("Через дополнительный аккаунт") and "Данные аккаунта запросим после оплаты" in r
    assert "variant_open" in brain.SITUATIONS and "Один вариант за него не выбирай" in brain.STORE_FACTS
    assert "про оба" in brain.VARIANT_OPEN_HINT


def test_variant_guard_after_owner_text():
    """Готовый текст №90 — только про доп. аккаунт: при невыбранном варианте про личный дописывается после него."""
    u = {"question": "", "facts": "", "situations": {"variant_open"}, "rules": [], "games": []}
    assert brain.variant_guard("{текст_90}", u, "а что от меня нужно для подключения?") == \
        "{текст_90}\n\n" + brain.VARIANT_PERSONAL
    assert brain.variant_guard("{текст_90}", u, "давайте через доп, что нужно?") == "{текст_90}"   # выбрал
    assert brain.variant_guard("{текст_90}", u, "что нужно?", paid=True) == "{текст_90}"
    both = "Через доп. — быть у консоли. На личный — почта и пароль после оплаты."
    assert brain.variant_guard(both, u, "сами подключаете?") == both
    assert brain.variant_guard("{текст_90}", None, "что нужно?") == "{текст_90}"   # разбора нет — как раньше


def test_who_enters_code_by_mode():
    """Макс 25.11: ручное подключение — клиент присылает код с экрана, вводим мы; автовыдача — код с почты присылаем мы."""
    assert "мы его вводим" in brain.ISSUE_HINT["off"] and "своя почта" in brain.ISSUE_HINT["off"]
    assert "код с почты этого аккаунта присылаем мы" in brain.ISSUE_HINT["creds"]
    assert "мы вводим этот код" in brain.ISSUE_HINT["self"]


def test_console_note_before_payment():
    """Макс 25.11: покупали с Xbox 360 — перед оплатой консоли строка о консолях; ПК — без неё; pay_text не меняется."""
    from shared import sales
    con = {"method": "ip", "product": "console", "pay_url": "https://qr.nspk.ru/x", "amount": 2290, "discount": 0}
    assert sales.pay_after(con).startswith(sales.CONSOLE_NOTE) and "360" in sales.CONSOLE_NOTE
    assert sales.pay_after({**con, "product": "pc"}) == sales.PAY_AFTER["ip"]
    assert "360" not in sales.pay_text(con)
    assert "Xbox 360" in brain.STORE_FACTS


def test_no_names_in_new_prompt_parts():
    """21.11: никаких имён в промпте — и в сегодняшних фактах и подсказках."""
    for text in (brain.STORE_FACTS, brain.GAME_INFO_HINT, brain.PRICE_DROP_HINT, brain.VARIANT_OPEN_HINT,
                 brain.UNDERSTAND_NOTE, *brain.ISSUE_HINT.values()):
        assert "Макс" not in text and "Леонид" not in text and "Диман" not in text


def test_variant_only_when_personal_offered():
    u = {"question": "", "facts": "", "situations": {"variant_open"}, "rules": [], "games": []}
    only_main = [("client", "как подключаете?"), ("seller", "Добавим аккаунт с подпиской, пришлёте код с экрана.")]
    both = [("client", "на мой акк?"), ("seller", "Через доп. аккаунт или на личный — цена другая.")]
    assert not brain.personal_offered(only_main) and brain.personal_offered(both)
    assert brain.variant_guard("{текст_90}", u, "код куда вводить?", offered=False) == "{текст_90}"
