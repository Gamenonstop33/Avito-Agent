"""Розыгрыш выключен (19.11, XBOXGARANT): о нём не говорим, хотя правила и база — общие с Максом."""
from services.dialog import brain
from services.dialog.brain import RAFFLE_NOW, drop_raffle


def test_hints_lose_raffle_only():
    adv = drop_raffle(brain.ADVICE_HINT)
    assert "розыгрыш" not in adv and "подарок от 10 мес" in adv
    ref = drop_raffle(brain.REFUSE_NEW_HINT)
    assert "розыгрыш" not in ref and "игра в подарок от 10 мес" in ref and "Акции" in ref


def test_reply_clauses_and_sentences():
    r = drop_raffle("На 14 мес — 5 090 ₽ 🎁 + розыгрыш. От 10 мес — игра в подарок, от 14 мес — ещё и участие в "
                    "розыгрыше топовой игры (GTA 6, EA FC 27). Оформляем?")
    assert "розыгрыш" not in r and "5 090 ₽" in r and "игра в подарок" in r and r.endswith("Оформляем?")
    r = drop_raffle("Подробности розыгрыша сообщим позже.\nНа какой срок оформляем?")
    assert r == "На какой срок оформляем?"


def test_asked_about_raffle():
    r = drop_raffle("Да, от 14 мес — участие в розыгрыше GTA 6. На какой срок Вас интересует?", "а розыгрыш есть?")
    assert r == f"{RAFFLE_NOW} На какой срок Вас интересует?"
    assert drop_raffle("Подробности розыгрыша сообщим позже.", "когда розыгрыш?") == RAFFLE_NOW


def test_untouched_without_raffle():
    t = "Здравствуйте! {прайс}"
    assert drop_raffle(t) == t
