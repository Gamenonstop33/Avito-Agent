from shared.kb_classes import classify_dialog, route


def test_classify_by_item():
    assert classify_dialog("Смена Региона Стим TR/UA/KZ", []) == "steam"
    assert classify_dialog("Игры Steam / Любые игры / Ключи /Россия", []) == "steam"
    assert classify_dialog("Подписка Xbox Game Pass Ultimate 17 месяцев", []) == "core"
    assert classify_dialog("Подписка / Игры на Xbox / game pass", []) == "core"
    assert classify_dialog("Forza Horizon 5 (xbox)", []) == "games"
    assert classify_dialog("Игры для Xbox / Ключи / Подписка", []) == "games"


def test_classify_personal_chat_by_text():
    assert classify_dialog(None, ["сможете поменять регион в стиме?"]) == "steam"
    assert classify_dialog(None, ["нужен ключ на гта 5"]) == "games"
    assert classify_dialog(None, ["подписка на полгода сколько?"]) == "core"


def test_route():
    assert route("а ключи стим продаёте?") == ["steam", "games"]
    assert route("есть форза?") == ["games"]
    assert route("на полгода сколько?") == []
