import asyncio

from services.owner_bot import issue

LINK = "https://www.avito.ru/profile/messenger/channel/u2i-8Xnxwu6OPV1nx_8L6KDTKA"


class FakeLLM:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail = answer or {}, fail

    async def chat_json(self, system, user, **kw):
        if self.fail:
            raise RuntimeError("LLM недоступна")
        return self.answer


def parse(text, **kw):
    return asyncio.run(issue.parse(FakeLLM(**kw), text))


def test_llm_normalizes_request():
    r = parse(f"{LINK} приставка два месяца", answer={"chat_id": "u2i-8Xnxwu6OPV1nx_8L6KDTKA", "product": "console",
                                                      "months": 2})
    assert r == {"chat_id": "u2i-8Xnxwu6OPV1nx_8L6KDTKA", "product": "console", "months": 2}


def test_chat_only_from_text_and_platform_by_text():
    # LLM ошиблась в чате и платформе — чат берём из текста, «пк» в тексте сильнее
    r = parse(f"{LINK} пк 4 месяца", answer={"chat_id": "u2i-chuzhoi", "product": "console", "months": 4})
    assert r == {"chat_id": "u2i-8Xnxwu6OPV1nx_8L6KDTKA", "product": "pc", "months": 4}


def test_pc_words():
    for words in ("на компе год", "компьютер 12 мес", "ноутбук 12 мес"):
        assert parse(f"{LINK} {words}", answer={"product": "console", "months": 12})["product"] == "pc", words
    assert parse(f"{LINK} комплект 2 мес", fail=True)["product"] == "console"


def test_regex_when_llm_down():
    assert parse(f"{LINK} пк 4 мес", fail=True) == {"chat_id": "u2i-8Xnxwu6OPV1nx_8L6KDTKA", "product": "pc",
                                                    "months": 4}
    assert parse(f"{LINK} 6м", fail=True)["product"] == "console"


def test_errors():
    assert "ссылку" in parse("приставка 2 месяца", fail=True)
    assert "срок" in parse(f"{LINK} приставка", fail=True)
    assert "несколько" in parse(f"{LINK} {LINK[:-3]}abc 2 мес", fail=True)


def test_u2u_chat():
    # аудит 20.10: чаты u2u- (не по объявлению) ручная выдача не находила
    r = parse("https://www.avito.ru/profile/messenger/channel/u2u-TsCBQaigbQbHM1nUACVmOg приставка 2 мес", fail=True)
    assert r == {"chat_id": "u2u-TsCBQaigbQbHM1nUACVmOg", "product": "console", "months": 2}
