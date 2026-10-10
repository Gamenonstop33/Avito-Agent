from datetime import datetime, timedelta, timezone

from services.avito_gateway.logic import chat_allowed, event_kind, origin_of, reply_delay, split_text

T0 = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def test_origin():
    assert origin_of({"id": "1", "direction": "in"}, set()) == "client"
    assert origin_of({"id": "2", "direction": "out"}, {"2"}) == "bot"
    assert origin_of({"id": "3", "direction": "out"}, set()) == "owner"
    assert origin_of({"id": "4", "direction": "out", "type": "system"}, set()) == "system"


def test_event_only_after_start():
    later, earlier = T0 + timedelta(seconds=1), T0 - timedelta(seconds=1)
    assert event_kind("client", later, None) is None           # бот остановлен
    assert event_kind("client", earlier, T0) is None           # старое сообщение
    assert event_kind("client", later, T0) == "client_message"
    assert event_kind("owner", later, T0) == "owner_message"
    assert event_kind("bot", later, T0) is None
    assert event_kind("owner", later, T0, "Сообщение удалено") is None


def test_owner_events_while_stopped():
    later = T0 + timedelta(minutes=1)
    assert event_kind("owner", later, None, "2300", stopped_at=T0) == "owner_message"   # чат за Максом
    assert event_kind("client", later, None, "Жду", stopped_at=T0) is None
    assert event_kind("owner", T0 - timedelta(minutes=1), None, "x", stopped_at=T0) is None


def test_whitelist_empty_means_nobody():
    assert not chat_allowed("x", False, [])
    assert chat_allowed("x", False, ["x"])
    assert chat_allowed("y", True, [])


def test_reply_delay_range():
    for _ in range(50):
        assert 5 <= (reply_delay(T0) - T0).total_seconds() <= 20


def test_delivery_failed_event():
    later = T0 + timedelta(seconds=1)
    assert event_kind("system", later, T0, "[Системное сообщение] ⛔️ Сообщение не отправилось: похоже…") == \
        "delivery_failed"
    assert event_kind("system", later, T0, "Вы можете оформить заказ онлайн") is None


def test_split_text():
    assert split_text("коротко") == ["коротко"]
    price = "\n".join(f"{m} мес — {m * 300} ₽" for m in range(1, 40))
    text = "🫶🏻Приветствуем\n\n" + price + "\n\n" + "слово " * 120
    parts = split_text(text)
    assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
    assert parts[0] == "🫶🏻Приветствуем\n\n" + price          # абзац прайса целиком в первой части
    assert " ".join(parts[1:]).split() == ("слово " * 120).split()
    assert split_text("x" * 2500) == ["x" * 1000, "x" * 1000, "x" * 500]
