from datetime import datetime, timezone

from services.dialog.lessons import render_chat, rules_block

T = datetime(2026, 10, 7, 16, tzinfo=timezone.utc)


def row(origin, text, deleted=False):
    return {"origin": origin, "text": text, "type": "text", "deleted_at": T if deleted else None}


def test_render_chat_marks_deleted_bot_and_masks():
    out = render_chat(3, [row("client", "Пароль просит от вашего аккаунта"),
                          row("bot", "После этого снова добавьте аккаунт по коду", deleted=True),
                          row("owner", "Пароль 56r7g94E, почта test@outlook.com"),
                          row("system", "служебное")])
    assert out.split("\n")[0] == "### Чат 3"
    assert "Б (удалено Максом): После этого" in out
    assert "56r7g94E" not in out and "test@outlook.com" not in out
    assert "служебное" not in out


def test_rules_block():
    assert rules_block([]) == ""
    assert "- Не пересказывай инструкцию" in rules_block(["Не пересказывай инструкцию"])


def test_lesson_similar():
    from services.dialog.lessons import similar
    known = ["Если клиент просит игру, которой нет в подписке, не называй цену сам — напиши, что цену уточнишь, "
             "и дождись ответа Макса."]
    assert similar("Если клиент спрашивает про отдельную покупку игры, которой нет в подписке, не называй цену сам — "
                   "уточни, что цену уточнишь, и дождись ответа Макса.", known)
    assert not similar("Если клиент отказывается от скидки за отзыв, указывай в реквизитах сумму без скидки.", known)
