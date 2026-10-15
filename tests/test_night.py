from datetime import datetime, timedelta, timezone

from services.dialog.night import in_workday, is_night, last_night, wait_action

MSK = timezone(timedelta(hours=3))


def at(h, m=0, day=29):
    return datetime(2026, 9, day, h, m, tzinfo=MSK)


def test_is_night():
    """Ночь бота 23–8 (14.10: Макс отвечает до 23); рассылки и сводки — 8–20."""
    assert is_night(at(23)) and is_night(at(3)) and is_night(at(7, 59))
    assert not is_night(at(8)) and not is_night(at(20)) and not is_night(at(22, 59))
    assert in_workday(at(8)) and in_workday(at(19, 59)) and not in_workday(at(20)) and not in_workday(at(23))


def test_last_night_window():
    start, end = last_night(at(8, 1))
    assert start == at(23, day=28) and end == at(8)
    start, end = last_night(at(7))             # ночь ещё идёт — берём предыдущую
    assert end == at(8, day=28)


def test_night_handoff_defers_at_once():
    since = at(23, 30)
    assert wait_action("handoff", since, at(23, 31)) == "defer"                    # без пингов: Макс спит
    assert wait_action("handoff", since, at(23, 50), deferred=at(23, 31)) is None


def test_day_case_at_nightfall():
    since = at(22, 40)                                       # висит с вечера, напоминание уже было
    assert wait_action("handoff", since, at(23, 0), at(22, 55)) == "defer"


def test_day_wait_one_reminder():
    """Днём — не больше двух алертов на чат: передача + одно напоминание через 15 мин."""
    since = at(12)
    assert wait_action("handoff", since, at(12, 14)) is None
    assert wait_action("handoff", since, at(12, 15)) == "ping"
    assert wait_action("handoff", since, at(12, 40), at(12, 15)) is None
    assert wait_action("handoff", since, at(13), at(12, 15)) == "nudge"           # клиенту «нужно время»
    assert wait_action("handoff", since, at(15), at(12, 15), at(13)) is None      # больше ничего
    assert wait_action("handoff", at(16), at(16, 15), at(12, 15), at(13)) == "ping"   # новое ожидание — заново


def test_night_owner_waits_hour():
    since = at(23, 10)                                       # Макс сам ведёт чат ночью
    assert wait_action("owner", since, at(23, 40)) is None
    assert wait_action("owner", since, at(0, 10, day=30)) == "defer"
    assert wait_action("owner", since, at(1, day=30), deferred=at(0, 10, day=30)) is None
