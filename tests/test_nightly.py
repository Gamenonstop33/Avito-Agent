from datetime import datetime

from services.dialog import night
from services.nightly import worker


def at(h, d=1):
    return datetime(2026, 10, d, h, 0, tzinfo=night.TZ)


def test_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "LAST", tmp_path / "last.txt")
    end = night.DAY_END
    assert worker.next_run(at(10)) == at(end)                 # днём — ждём конца дня
    assert worker.next_run(at(end + 1)) == at(end + 1)        # сегодня не было — сразу
    worker.LAST.write_text("2026-10-01")
    assert worker.next_run(at(end + 1)) == at(end, 2)         # уже было — завтра
