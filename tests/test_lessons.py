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


class FakeLLM:
    def __init__(self, res):
        self.res = res

    async def chat_json(self, system, user, **kw):
        if isinstance(self.res, Exception):
            raise self.res
        return self.res


def test_check_same_and_conflict():
    import asyncio
    from services.dialog.lessons import check
    rows = [{"id": 88, "rule": "ЛИЧНЫЙ — прайс доп. аккаунта"}, {"id": 36, "rule": "предложи купить отдельно"}]
    res = {"same": [{"id": 88, "why": "одно и то же"}, {"id": 999}], "conflict": [36, "88", "x"]}
    same, off = asyncio.run(check(FakeLLM(res), "новое", rows))
    assert [r["id"] for r in same] == [88]          # чужие номера отбрасываются
    assert [r["id"] for r in off] == [36]           # повтор не считается ещё и противоречием
    assert asyncio.run(check(FakeLLM(RuntimeError("402")), "новое", rows)) == ([], [])   # сбой — не блокируем
    assert asyncio.run(check(FakeLLM(res), "новое", [])) == ([], [])


STEPS = ("Если ошибка сети — отправь клиенту этот текст:\n\n1. Сменить регион:\n"
         "Настройки → Система   → США\n\n\n\nосновной\n111.88.96.50")


def test_rewrite_keeps_owner_steps():
    """03.11: шаги инструкции владельца не сжимаются в одну строку и не режутся на 400 символах."""
    import asyncio
    from services.dialog.lessons import RULE_MAX, rewrite
    out = asyncio.run(rewrite(FakeLLM({"rule": STEPS + "\n" + "шаг " * 200}), "старое", STEPS))
    assert out.startswith("Если ошибка сети — отправь клиенту этот текст:\n\n1. Сменить регион:\n"
                          "Настройки → Система → США\n\nосновной\n111.88.96.50")
    assert 400 < len(out) <= RULE_MAX
    assert asyncio.run(rewrite(FakeLLM(RuntimeError("402")), "", STEPS)).count("\n") == 6   # сбой — текст владельца


def test_rules_block_multiline():
    block = rules_block(["Если ошибка сети — отправь:\n1. Регион\n2. DNS", "Не спорь"])
    assert "- Если ошибка сети — отправь:\n  1. Регион\n  2. DNS\n- Не спорь" in block


def test_clash_text():
    from services.owner_bot.main import clash_text
    assert clash_text([{"id": 18, "rule": "без интернета"}]) == "⚠️ Похоже, спорит с действующим:\n«без интернета»"
    assert "1. «а»\n2. «б»" in clash_text([{"id": 1, "rule": "а"}, {"id": 2, "rule": "б"}])


NET = ('Если ошибка сети, не давай своих шагов — отправь клиенту этот текст:\n"1. Сменить регион:\n'
       'Настройки → Система → США\n\nосновной\n111.88.96.50"')


def test_ready_text_marker_and_fill():
    """03.11: готовый текст владельца — метка {текст_N} в промпте, клиенту — дословно, с переносами, без кавычек."""
    from services.dialog.lessons import fill_texts, ready_text, texts_hint
    situation, text = ready_text(NET)
    assert situation == "Если ошибка сети, не давай своих шагов"
    assert text == "1. Сменить регион:\nНастройки → Система → США\n\nосновной\n111.88.96.50"
    assert ready_text("Не спорь с клиентом") == ("Не спорь с клиентом", "")
    rules = ["Не спорь с клиентом", NET]
    block = rules_block(rules)
    assert "{текст_2}" in block and "111.88.96.50" not in block and "- Не спорь с клиентом" in block
    assert "- {текст_2} — Если ошибка сети" in texts_hint(rules) and texts_hint(["Не спорь"]) == ""
    assert fill_texts("Сделайте так: {текст_2}. {текст_2}", rules) == "Сделайте так:\n\n" + text
    assert fill_texts("{текст_1} {текст_9} Уточню", rules) == "Уточню"   # без текста и чужой номер — прочь
    assert fill_texts("Обычный ответ", rules) == "Обычный ответ" and fill_texts("", []) == ""


def test_rewrite_numbers_untouched():
    """03.11: правка орфографии не меняет числа — DNS с другой цифрой не сохраняем, берём текст владельца."""
    import asyncio
    from services.dialog.lessons import rewrite
    owner = "Если ошибка сети — отправь: оснвоной 111.88.96.50"
    fixed = asyncio.run(rewrite(FakeLLM({"rule": "Если ошибка сети — отправь клиенту этот текст:\nосновной 111.88.96.50"}),
                                "", owner))
    assert "основной 111.88.96.50" in fixed
    assert asyncio.run(rewrite(FakeLLM({"rule": "отправь клиенту этот текст:\nосновной 111.88.96.51"}), "", owner)) == owner
