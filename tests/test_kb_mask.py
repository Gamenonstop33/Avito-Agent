from services.dialog.build_kb import mask


def test_mask():
    s = mask("Логин: donor123@outlook.com пароль: Qwe12345 тел +7 (912) 345-67-89")
    assert "donor123" not in s and "Qwe12345" not in s and "345-67-89" not in s
    assert mask("Вас на какой срок интересует?") == "Вас на какой срок интересует?"


def test_mask_keeps_plain_words():
    assert mask("там будет код напишите его") == "там будет код напишите его"
    assert "4821" not in mask("код 4821")


def test_mask_cards_and_credentials():
    s = mask("2202206122252252 Соколов К. Е; VPN S4M user@mail.ru:Vinay@6666")
    assert "2202206122252252" not in s and "Vinay" not in s


def test_secret_templates_excluded():
    from services.dialog.build_kb import SECRETS
    assert SECRETS.search(mask("💳ОПЛАТА💳 СБП по номеру телефона СБЕРБАНК +7 912 345-67-89"))
    assert not SECRETS.search(mask("Вас на какой срок интересует?"))


def test_mask_password_after_email_with_emoji():
    assert "Vinay" not in mask("VPN (общий аккаунт) 💚user@mail.ru 💚Vinay@6666 Выберите")
