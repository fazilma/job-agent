from datetime import datetime, time, timedelta, timezone

from jobagent import daily

DXB = timezone(timedelta(hours=4))
AT = time(7, 30)


def t(day, hh, mm):
    return datetime(2026, 10, day, hh, mm, tzinfo=DXB)


def test_waits_until_the_run_time():
    assert not daily.is_due(t(17, 7, 29), AT, t(16, 7, 30), None)
    assert daily.is_due(t(17, 7, 30), AT, t(16, 7, 30), None)


def test_runs_once_per_day():
    assert not daily.is_due(t(17, 9, 0), AT, t(17, 7, 30), t(17, 7, 30))


def test_catches_up_after_sleep_or_container_down():
    # container started at 11:00, last run was yesterday -> run now
    assert daily.is_due(t(17, 11, 0), AT, t(16, 7, 31), None)
    # never ran at all
    assert daily.is_due(t(17, 11, 0), AT, None, None)


def test_manual_run_after_the_slot_counts():
    assert not daily.is_due(t(17, 12, 0), AT, t(17, 8, 0), None)


def test_failed_attempt_retries_after_an_hour():
    # attempt at 07:30 left no run row (e.g. no internet)
    assert not daily.is_due(t(17, 8, 0), AT, t(16, 7, 30), t(17, 7, 30))
    assert daily.is_due(t(17, 8, 31), AT, t(16, 7, 30), t(17, 7, 30))


def test_utc_run_times_compare_correctly():
    # runs.started_at is stored in UTC: 03:45 UTC = 07:45 Dubai, after the slot
    started = datetime(2026, 10, 17, 3, 45, tzinfo=timezone.utc)
    assert not daily.is_due(t(17, 9, 0), AT, started, None)


def test_parse_at():
    assert daily.parse_at("07:30") == time(7, 30)
    assert daily.parse_at(" 6:05") == time(6, 5)


def test_loop_launches_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(daily, "last_run_start", lambda p: None if not calls else datetime.now().astimezone())
    th = daily.start(time(0, 0), tmp_path / "x.db", lambda: calls.append(1) or True, check_every=0.01)
    import time as _t
    _t.sleep(0.2)
    th.stop.set()
    assert len(calls) == 1


def test_next_run():
    assert daily.next_run(t(17, 6, 0), AT, t(16, 7, 30), None) == t(17, 7, 30)        # later today
    assert daily.next_run(t(17, 9, 0), AT, t(17, 7, 31), None) == t(18, 7, 30)        # done -> tomorrow
    assert daily.next_run(t(17, 9, 0), AT, t(16, 7, 30), None) == t(17, 9, 0)         # missed -> now
    # failed attempt at 07:30 (no run row): retry at 08:30
    assert daily.next_run(t(17, 8, 0), AT, t(16, 7, 30), t(17, 7, 30)) == t(17, 8, 30)
