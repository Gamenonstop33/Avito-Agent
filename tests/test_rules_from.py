"""RULES_FROM (19.11, XBOXGARANT): правила и база знаний — с сервера Макса; здесь их не предлагаем и не правим."""
import asyncio
import importlib
import json

import pytest


@pytest.fixture
def mirror(monkeypatch):
    monkeypatch.setenv("RULES_FROM", "GameNonStop")
    import shared.config
    import services.owner_bot.main as ob
    import services.dialog.lessons as lessons
    mods = [importlib.reload(shared.config), importlib.reload(ob), importlib.reload(lessons)]
    yield mods
    monkeypatch.delenv("RULES_FROM")
    for m in (shared.config, ob, lessons):
        importlib.reload(m)


def test_owner_bot_read_only(mirror):
    _, ob, _ = mirror
    assert "rule_add" not in json.dumps(ob.MENU, ensure_ascii=False)
    assert "✍️ Добавить правило" not in ob.HELP
    assert "общие с магазином GameNonStop" in ob.HELP


def test_lessons_skipped(mirror, monkeypatch, capsys):
    _, _, lessons = mirror

    async def no_db(*a, **k):
        raise AssertionError("в базу не ходим")
    monkeypatch.setattr(lessons, "connect", no_db)
    asyncio.run(lessons.main())
    assert "не собираю" in capsys.readouterr().out


def test_default_unchanged():
    import services.owner_bot.main as ob
    assert "rule_add" in json.dumps(ob.MENU, ensure_ascii=False)
    assert "✍️ Добавить правило" in ob.HELP
