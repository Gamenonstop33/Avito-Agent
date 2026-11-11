"""02.11: игнор между аккаунтами заказчика, последняя инструкция клиента, «подписка слетела», база знаний с 01.03."""
import re
from pathlib import Path

from services.dialog import brain, build_kb
from shared import sales
from shared.db import ns_sql

ROOT = Path(__file__).resolve().parents[1]


def test_own_accounts_ignored_both_ways():
    """В чатах Avito аккаунты видны под другими номерами, чем в API: игнор — по обоим, в обеих копиях."""
    schema = (ROOT / "services/avito_gateway/schema.sql").read_text(encoding="utf-8")
    ids = {int(x) for x in re.findall(r"\((\d{6,})\s*,", schema)}
    assert {390946417, 500637610, 327687164, 319641330} <= ids
    import shared.db as db
    old = db.NS
    try:
        db.NS = "2"
        assert "gateway2.ignored" in ns_sql(schema) and "gateway.ignored" not in ns_sql(schema)
    finally:
        db.NS = old


def test_last_slot_takes_latest_of_orders_and_messages():
    q = sales.LAST_SLOT
    assert "UNION ALL" in q and "ORDER BY at DESC" in q   # одна выборка по времени, а не «сначала заказы бота»


def test_fell_question():
    for t in ("подписка слетела", "у меня всё отлетело", "игры не запускаются", "гейм пасс пропал",
              "просит купить подписку", "меня выкинуло из аккаунта", "пропала подписка"):
        assert brain.FELL_Q.search(t), t
    for t in ("сколько стоит на 6 месяцев", "есть ли гта 5", "спасибо, всё работает"):
        assert not brain.FELL_Q.search(t), t


def test_fell_hint_only_first_place_with_recent_chat():
    assert brain.fell_hint(1, True) == brain.FELL_OK
    for slot, active in ((2, True), (None, True), (1, False), (2, False)):
        hint = brain.fell_hint(slot, active)
        assert "handoff problem" in hint and "{галочка}" in hint and "ответь только" not in hint


def test_fell_guard():
    assert brain.fell_guard("{галочка}", None, True) == ("{галочка}", None)
    reply, h = brain.fell_guard("{галочка}", None, False)
    assert reply == brain.FELL_HANDOFF and h["reason"] == "problem"
    reply, h = brain.fell_guard("Войдите в аккаунт с подпиской и поставьте галочку домашней консоли.", None, False)
    assert h and h["reason"] == "problem"   # расписала галочку сама — второму месту нельзя
    reply, h = brain.fell_guard("Уточню и напишу Вам {галочка}", {"reason": "problem"}, True)
    assert "{галочка}" not in reply and h["reason"] == "problem"   # передача — без инструкции
    assert brain.fell_guard("Сейчас проверим.", None, False) == ("Сейчас проверим.", None)


def test_home_fix_rendered_once():
    text = brain.render_reply("{галочка}", "прайс")
    assert text.startswith("Войдите в аккаунт с подпиской") and "Моя домашняя консоль Xbox" in text
    assert "{галочка}" not in text and "прайс" not in text


def test_rules_connection_only_approved():
    assert "17. Подключение — только по двум утверждённым инструкциям" in brain.RULES
    assert "занимает 5–10 минут" in brain.RULES and "ПОДПИСКА СЛЕТЕЛА" in brain.RULES


def test_kb_since_march_and_no_old_instructions():
    assert build_kb.SINCE == 1772312400   # 01.03.2026 00:00 МСК
    old = ("далее пишите любой тег и жмете далее нет спасибо далее далее Без ограничений пропустить сделать домашней",
           "Сперва отключите НА ВСЕХ своих аккаунтах ДОМАШНЮЮ КОНСОЛЬ",
           "сменить регион на США → очистить MAC-адрес → прописать DNS")
    assert all(build_kb.CONNECT_STEPS.search(t) for t in old)
    assert not build_kb.CONNECT_STEPS.search("Спасибо за покупку! Будем рады отзыву 🙏")
    pm = build_kb.Source("PREMIUM MARKET", "2", by_class={"core": []})
    text = build_kb.assemble("core", {k: "x" for k in build_kb.REDUCE}, *build_kb.stats("core", [pm]), "прайс")
    assert "## Подключение (утверждено владельцем)" in text and "5–10 минут" in text
