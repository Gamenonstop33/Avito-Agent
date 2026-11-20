from shared.crm import is_purchase_msg


def test_purchase_markers():
    assert is_purchase_msg("1) Нажмите кнопку Xbox на Джойстике, и ведите вправо до конца")
    assert is_purchase_msg("Если все ок оставьте отзыв)")
    assert not is_purchase_msg("Для новых клиентов делаем скидку за отзыв 🌟 Вас на какой срок интересует?")
    assert not is_purchase_msg("Как оплатите пришлите скрин")


def test_purchase_markers_games_steam():
    assert is_purchase_msg("Инструкция для активации ключа XBOX ______")
    assert is_purchase_msg("❗Рекомендации❗ 24 часа, после смены: не менять данные")
    assert is_purchase_msg("Буду благодарен,если оставите отзыв и подпишитесь на группу")
    assert not is_purchase_msg("После оплаты предоставлю вам ключ с игрой")
