import asyncio
from contextlib import asynccontextmanager

from services.dialog import kb_rules as K

KB = ("# База знаний\n\nВступление.\n\n## Прайс (из БД на момент сборки)\n\n2 мес — 1000 ₽\n\n"
      "## Частые вопросы\n\n#### Как удалить свой аккаунт с консоли\nПросто удалите свой аккаунт.\n\n"
      "#### Есть ли подарок\nДа, от 10 мес.\n\n## Когда звать Макса\n\n"
      + "".join(f"- Случай {i}: передать Максу, если клиент пишет про это и ничего больше не помогает.\n"
                for i in range(40)) + "- Повтор.\n\n- Повтор.\n")


class Con:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO dialog.kb_edits"):
            self.db.edits.append(dict(zip(("rule_id", "file", "part", "old", "new", "nth"), a),
                                      id=len(self.db.edits) + 1, undone_at=None))
        elif sql.endswith("WHERE undone_at IS NULL"):   # built: база собрана заново
            for e in self.db.edits:
                e["undone_at"] = e["undone_at"] or 1
        elif sql.startswith("UPDATE dialog.rules SET kb_text = CASE"):
            self.db.built = list(a[0])
        elif sql.startswith("UPDATE dialog.kb_edits SET undone_at"):
            next(e for e in self.db.edits if e["id"] == a[0])["undone_at"] = 1
        elif sql.startswith("UPDATE dialog.rules SET kb_text=NULL"):
            self.db.reset |= set(a[0])
        elif sql.startswith("UPDATE dialog.rules SET kb_text"):
            self.db.kb_text[a[0]] = a[1]


class DB:
    def __init__(self):
        self.edits, self.reset, self.kb_text = [], set(), {}

    @asynccontextmanager
    async def acquire(self):
        yield Con(self)

    async def fetch(self, sql, *a):
        return [e for e in self.edits if e["undone_at"] is None]


def setup(tmp_path, monkeypatch, text=KB):
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "kb_core.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(K.brain, "ROOT", tmp_path)
    return DB()


def edit(monkeypatch, fn):
    """propose без LLM: fn(куски) → [(кусок, новый текст тела)]."""
    async def propose(llm, rule, *a):
        items = K.blocks("core", K.read("core"))
        return [(b, b["head"] + new + "\n\n") for b, new in fn(items)]
    monkeypatch.setattr(K, "propose", propose)


def find(items, start):
    return next(b for b in items if b["body"].lstrip().startswith(start))


def test_blocks_positions_skip_price_and_split_big():
    items = K.blocks("core", KB)
    assert all(KB[b["at"]:b["at"] + len(b["old"])] == b["old"] for b in items)
    assert not any("1000 ₽" in b["body"] for b in items)   # прайс — из БД, не правим
    big = [b for b in items if b["part"] == "Когда звать Макса"]
    assert len(big) > 1 and all(len(b["body"]) <= K.PIECE * 1.5 for b in big)
    assert find(items, "Просто удалите")["part"] == "Частые вопросы › Как удалить свой аккаунт с консоли"


def test_apply_then_undo_restores_text(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch)
    edit(monkeypatch, lambda items: [(find(items, "Просто удалите"), "Этот вопрос решает Максим — передать ему.")])
    asyncio.run(K.apply(db, None, 7, "Как удалить аккаунты — передай Максу"))
    assert "передать ему" in K.read("core") and "Просто удалите" not in K.read("core")
    assert db.kb_text[7].startswith("Как удалить")
    asyncio.run(K.undo(db, 7))
    assert K.read("core") == KB and db.reset == {7}


def test_duplicate_paragraphs_edit_only_ours(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch, KB.replace("- Повтор.\n\n- Повтор.\n", "") + "\n## Ещё\n\nПовтор.\n\n"
               "## И ещё\n\nПовтор.\n")
    edit(monkeypatch, lambda items: [([b for b in items if b["body"].strip() == "Повтор."][1], "Повтор, но иначе.")])
    asyncio.run(K.apply(db, None, 1, "правило"))
    text = K.read("core")
    assert text.index("## Ещё\n\nПовтор.\n") < text.index("Повтор, но иначе.")
    asyncio.run(K.undo(db, 1))
    assert K.read("core").endswith("## И ещё\n\nПовтор.\n")


def test_undo_takes_later_edits_of_same_section(tmp_path, monkeypatch):
    db = setup(tmp_path, monkeypatch)
    edit(monkeypatch, lambda items: [(find(items, "Да, от 10"), "Да, от 10 мес, только первая покупка.")])
    asyncio.run(K.apply(db, None, 1, "правило 1"))
    edit(monkeypatch, lambda items: [(find(items, "Да, от 10"), "Да, от 10 мес, только первая покупка, на доп. аккаунт."),
                                     (find(items, "Просто удалите"), "Передать Максиму.")])
    asyncio.run(K.apply(db, None, 2, "правило 2"))
    asyncio.run(K.undo(db, 1))   # правка 2 лежит поверх правки 1 — снимаются обе, правило 2 поправит заново
    assert K.read("core") == KB and db.reset == {1, 2}


def test_good_rejects_damage():
    old = "Текст раздела. " * 40
    assert K.good(old, old + "Добавка.")
    assert not K.good(old, "Коротко.")                         # обрезано
    assert not K.good(old, old + '{"reply": "Здравствуйте"}')   # ответ клиенту вместо справки
    assert not K.good(old, old)


def test_built_forgets_edits_and_marks_rules():
    """31.10: build_kb вписывает правила владельца в базу сама — старые правки забыты, правила повторно не правят."""
    db = DB()
    db.edits = [{"id": 1, "undone_at": None}, {"id": 2, "undone_at": 5}]
    asyncio.run(K.built(db, ["правило А", "правило Б"]))
    assert [e["undone_at"] for e in db.edits] == [1, 5] and db.built == ["правило А", "правило Б"]


def test_gone_rule_is_cut_without_undo_record(tmp_path, monkeypatch):
    """31.10: правило, вписанное сборкой, убрали — его текст вычищается, «было/стало» нет (откат вернул бы его)."""
    db = setup(tmp_path, monkeypatch)
    edit(monkeypatch, lambda items: [(find(items, "Да, от 10"), "")])
    asyncio.run(K.apply(db, None, 9, "Подарок — от 10 мес", gone=True))
    assert "Да, от 10" not in K.read("core") and "#### Есть ли подарок" in K.read("core")
    assert db.edits == [] and db.kb_text[9] is None


def test_cut_only_removes():
    old = "Абзац один. " * 30 + "\n\nПо правилу: передать Максу.\n"
    assert K.cut(old, old.replace("По правилу: передать Максу.\n", ""))
    assert not K.cut(old, old + "Добавка.")                     # вычистка не дописывает
    assert not K.cut(old * 5, "Коротко.")                      # из большого куска снесено почти всё
    assert not K.cut(old, old)


def test_drop_removes_orphan_heading(tmp_path, monkeypatch):
    """Ответ был только по отменённому правилу — убирается и заголовок вопроса (стенд 31.10: пустые «#### …»)."""
    setup(tmp_path, monkeypatch)
    gift = find(K.blocks("core", K.read("core")), "Да, от 10")

    class LLM:
        async def chat_json(self, system, user, **kw):
            if system is K.PICK:
                return {"ids": [gift["id"]], "home": gift["id"], "words": []}
            assert "ДЕЙСТВУЮЩИЕ" in user
            return {"blocks": [{"id": gift["id"], "text": ""}]}
    (b, new), = asyncio.run(K.propose(LLM(), "Подарок — от 10 мес", K.DROP, "ДЕЙСТВУЮЩИЕ ПРАВИЛА ВЛАДЕЛЬЦА:\n\n"))
    assert b["old"].startswith("#### Есть ли подарок") and new == ""
