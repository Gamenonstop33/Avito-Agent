from datetime import datetime, timedelta, timezone

from services.dialog.night import is_night, last_night, wait_action

MSK = timezone(timedelta(hours=3))


def at(h, m=0, day=29):
    return datetime(2026, 9, day, h, m, tzinfo=MSK)


def test_is_night():
    assert is_night(at(23)) and is_night(at(3)) and is_night(at(7, 59)) and is_night(at(20))
    assert not is_night(at(8)) and not is_night(at(19, 59))


def test_last_night_window():
    start, end = last_night(at(8, 1))
    assert start == at(20, day=28) and end == at(8)
    start, end = last_night(at(7))             # ночь ещё идёт — берём предыдущую
    assert end == at(8, day=28)


def test_night_handoff():
    since = at(23)
    assert wait_action("handoff", since, at(23, 3)) is None
    assert wait_action("handoff", since, at(23, 5)) == "ping"
    assert wait_action("handoff", since, at(23, 10), at(23, 5)) is None
    assert wait_action("handoff", since, at(23, 15), at(23, 5)) == "defer"          # 10 мин после повтора
    assert wait_action("handoff", since, at(23, 50), at(23, 5), deferred=at(23, 15)) is None


def test_day_case_at_nightfall_repings_first():
    since = at(19, 8)                                        # висит с дня, днём уже пинговали
    assert wait_action("handoff", since, at(20, 0), at(19, 8)) == "ping"
    assert wait_action("handoff", since, at(20, 5), at(20, 0)) is None             # не сразу «ответим утром»
    assert wait_action("handoff", since, at(20, 10), at(20, 0)) == "defer"


def test_day_wait():
    since = at(12)
    assert wait_action("owner", since, at(12, 59)) is None                         # меньше часа — ждём Макса
    assert wait_action("owner", since, at(13)) == "nudge"                          # «нужно время» + пинг
    assert wait_action("owner", since, at(13, 30), at(13), at(13)) is None
    assert wait_action("owner", since, at(14), at(13), at(13)) == "ping"           # раз в час до конца дня
    assert wait_action("handoff", since, at(13, 30), at(12)) == "nudge"
    assert wait_action("owner", at(14), at(15), at(13), at(13)) == "nudge"         # новое ожидание — заново


def test_night_owner_waits_hour():
    since = at(21)                                           # Макс сам ведёт чат вечером
    assert wait_action("owner", since, at(21, 30)) is None
    assert wait_action("owner", since, at(22)) == "defer"
    assert wait_action("owner", since, at(23), deferred=at(22)) is None
