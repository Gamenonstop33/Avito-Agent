"""«Цены в сообщении выше 👆» при прайсе в этом же сообщении убирается (18.11, XBOXGARANT, тестовый чат)."""
from services.dialog.brain import drop_price_above

PRICE = "🎮 XBOX ULTIMATE дом консоль (играете со своего аккаунта)\n2 мес — 1 090₽\n4 мес — 1 790₽"
OWN = "👤 На Ваш личный аккаунт\n4 мес — 2 490₽"


def test_pointer_dropped_when_price_below():
    text = ("Здравствуйте! Цены в сообщении выше 👆 На какой срок Вас сориентировать? Если брать больше — "
            "выходит выгоднее за месяц: посмотрите 4 или 6 мес.\n\n" + PRICE)
    out = drop_price_above(text, PRICE)
    assert "выше" not in out
    assert out.startswith("Здравствуйте! На какой срок Вас сориентировать?")
    assert out.endswith(PRICE)


def test_pointer_kept_when_price_not_in_message():
    text = "Цены — в сообщении выше 👆 На 6 мес — 2 290₽."
    assert drop_price_above(text, PRICE) == text
    text2 = "Подключаем через доп. аккаунт (цены — в сообщении выше 👆)."
    assert drop_price_above(text2, PRICE) == text2


def test_personal_pointer_kept_with_main_price():
    text = "Цены на личный аккаунт — в сообщении выше 👆\n\n" + PRICE
    assert drop_price_above(text, PRICE, OWN) == text
    both = "Цены на личный аккаунт — в сообщении выше 👆\n\n" + OWN
    assert "выше" not in drop_price_above(both, PRICE, OWN)
