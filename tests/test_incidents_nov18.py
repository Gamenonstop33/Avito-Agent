"""Прод 16–18.11 (замечания Макса 18.11): «на мой аккаунт?» — не личный, пока клиент прямо не выбрал; «12+1» — бонусов
нет; приветствие с обоими прайсами без повисших «прайсу:»; отказ — одно напоминание, постоянным 100 ₽."""
from services.dialog import brain
from services.dialog.brain import implied_order, own_unclear, personal_said, term_product

PRICES = {"console": {2: 1090, 4: 1790, 6: 2290, 12: 4290, 14: 5090}, "pc": {1: 400, 10: 1800},
          "personal": {4: 4190, 6: 4890, 12: 8490, 14: 9690}}
GNS = ["Доброе утро. Активация подписки на мой аккаунт?", "Получается в онлайне играть нельзя?",
       "Даже через без активации на свой аккаунт?", "Понял. После оплаты отправляете инструкцию?", "Ок.", "12"]


def test_gns_my_account_is_not_personal():
    """GNS 18.11: «на мой аккаунт?» → «12» ушло как личный 8 490 ₽ (+ допродажа 14 мес на личный)."""
    assert not personal_said(GNS) and own_unclear(GNS)
    assert term_product(12, " / ".join(GNS), PRICES) == "console"
    h = [("client", t) if i % 2 == 0 else ("seller", "…") for i, t in enumerate(sum(([t, "…"] for t in GNS), []))]
    assert implied_order([x for x in h if x[0] == "client"] , PRICES) is None
    # сказал «не на свой» — вариант выбран: доп. аккаунт
    done = GNS + ["Я не на свой аккаунт буду покупать"]
    assert not personal_said(done) and not own_unclear(done)


def test_premium_individual_is_personal():
    """PREMIUM 16.11: «индивидуальной подписки на мой старый аккаунт» — бот дал прайс «дом консоль»."""
    first = "Доброго дня. Подскажите пожалуйста по стоимости индивидуальной подписки на мой старый аккаунт"
    assert personal_said(first) and not own_unclear(first)
    assert personal_said("Домашняя консоль не интересует, необходима подписка на мой старый аккаунт")
    assert term_product(12, first + " / На 12 мес", PRICES) == "personal"
    assert personal_said(["можно на мой аккаунт?", "на свой"])          # ответ на «какой вариант?»
    assert not personal_said(["на личный?", "нет, давайте через доп"])   # последнее слово клиента решает


def test_bonus_month_is_refused():
    """PREMIUM 16.11: «Это будет 12+1 мес?» — «Да, 12 мес подписки + 1 мес бонусом»."""
    q = "Это будет 12+1 мес?"
    assert brain.BONUS_Q.search(q) and brain.BONUS_Q.search("Было же 12 + 1 месяц в подарок?)")
    assert brain.bonus_guard("Да, 12 мес подписки + 1 мес бонусом.", q, "personal") == brain.BONUS_REPLY["personal"]
    assert brain.bonus_guard("Да, конечно.", q, "new") == brain.BONUS_REPLY["new"]
    assert brain.bonus_guard("Нет, подписка ровно на 12 мес.", q, "new") == "Нет, подписка ровно на 12 мес."
    # «есть акции?» — «да, скидка за отзыв» не трогаем: это правда
    assert brain.bonus_guard("Да, новым — скидка 100 ₽ за отзыв.", "а акции есть?", "new").startswith("Да, новым")
    assert not brain.BONUS_Q.search("хочу на 12 месяцев")


def test_greeting_both_prices_order():
    """GNS 18.11: «цена по основному прайсу: Если нужно именно на личный аккаунт — цена другая:» и личный перед
    основным."""
    r = ("Подключаем через дополнительный аккаунт с подпиской — играете со своего профиля со всеми сохранениями, цена "
         "по основному прайсу: {прайс}. Если нужно именно на личный аккаунт — цена другая: {прайс_личный}")
    text = brain.render_reply(r, "[ОСНОВНОЙ]", first=True, new=True, personal="[ЛИЧНЫЙ]")
    assert "прайсу:" not in text and "цена другая:\n\n[ЛИЧНЫЙ]" in text
    assert text.index("[ОСНОВНОЙ]") < text.index("Если нужно именно на личный") < text.index("[ЛИЧНЫЙ]")


def test_refusal_reminder_once():
    """GNS 17.11: «пока не имею финансовой возможности» — одно напоминание; повтор — без продаж; постоянным 100 ₽."""
    assert brain.hesitates("В данный момент пока не имею финансовой возможности")
    h = [("seller", "12 мес — 4 290 ₽"), ("client", "пока нет денег")]
    assert brain.refuse_stage(h, True) == "" and brain.refuse_stage(h, False) == ""
    h2 = h + [("seller", "Понимаю! Акции временные — потом может не быть."), ("client", "нет, спасибо")]
    assert brain.refuse_stage(h2, True) == "again"
    h3 = h + [("seller", brain.LOYAL_NUDGE)]
    assert brain.refuse_stage(h3, False) == "again" and brain.discount_asked(h3)
    assert brain.discount_asked([("seller", "12 мес — 4 290 ₽"), ("client", "подумаю")])   # постоянный — можно 100 ₽
    assert "пропустили" not in brain.REFUSE_NEW_HINT.replace("«Вы пропустили / не заметили»", "")


def test_after_payment_future_is_not_paid_claim():
    """Стенд 18.11: «После оплаты отправляете инструкцию?» → «Да, после оплаты пришлём инструкцию» вырезалось как «оплата
    пришла», клиенту уходило «Оплату проверяем»."""
    assert not brain.PAID_CLAIM.search("Да, после оплаты пришлём инструкцию.")
    assert not brain.PAID_CLAIM.search("После оплаты получите доступ.")
    assert brain.PAID_CLAIM.search("Оплата пришла, спасибо!") and brain.PAID_CLAIM.search("Оплата получена")
    assert brain.PAID_CLAIM.search("Оплата подтверждена")


def test_not_own_account_means_main():
    price = "🎮 XBOX ULTIMATE дом консоль\n12 мес — 4 290₽\n\n💻 ПК\n10 мес — 1 800₽"
    h = [("client", "на мой аккаунт?"), ("seller", "…"), ("client", "12")]
    r = brain.main_after_no("Понял, тогда 12 мес на личный аккаунт — 8 490 ₽. Оформляем?",
                            "Я не на свой аккаунт буду покупать", h, price, True)
    assert r.startswith(brain.OWN_MAIN) and "4 190 ₽" in r and "8 490" not in r
    ok = "Отлично, 12 мес — 4 190 ₽ со скидкой. Оформляем?"
    assert brain.main_after_no(ok, "Я не на свой аккаунт буду покупать", h, price, True) == ok
    assert brain.own_term_text(12, {"console": {12: 4290}, "personal": {12: 8490}}, True) == (
        "12 мес через дополнительный аккаунт — 4 290 ₽ (для Вас со скидкой за отзыв — 4 190 ₽), на Ваш личный "
        "аккаунт — 8 490 ₽. Какой вариант подходит?")
