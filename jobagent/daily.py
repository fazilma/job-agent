"""Daily schedule for Docker (on the Mac, `jobagent schedule` uses launchd instead).

Runs inside the dashboard process (`jobagent serve --daily 07:30`) and starts the same
`jobagent run` the "Run now" button does. Every minute it checks the clock, so a run
missed while the container was stopped or the Mac was asleep happens as soon as it's
back, once per day.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, time, timedelta
from typing import Callable

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(hours=1)     # e.g. no internet at 07:30: try again an hour later

# What the dashboard shows on its System page. Filled in by start().
STATE: dict = {"enabled": False, "at": None, "last_attempt": None, "last_check": None}


def parse_at(text: str) -> time:
    hh, mm = text.strip().split(":")
    return time(int(hh), int(mm))


def is_due(now: datetime, at: time, last_run_start: datetime | None,
           last_attempt: datetime | None) -> bool:
    """True when today's run time has passed and nothing has run since then.

    `now` is local time (the container's TZ). `last_run_start` comes from the runs table
    (scheduled or manual), `last_attempt` is the scheduler's own last try, which may have
    ended without a run row (no network, a crash).
    """
    slot = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if now < slot:
        return False
    if last_run_start is not None and last_run_start >= slot:
        return False
    if last_attempt is not None and last_attempt >= slot and now - last_attempt < RETRY_AFTER:
        return False
    return True


def next_run(now: datetime, at: time, last_run_start: datetime | None,
             last_attempt: datetime | None) -> datetime:
    """When the scheduler will next start a run (now, if one is due)."""
    if is_due(now, at, last_run_start, last_attempt):
        return now
    slot = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if now < slot:
        return slot
    ran_today = last_run_start is not None and last_run_start >= slot
    if not ran_today and last_attempt is not None and last_attempt >= slot:
        return last_attempt + RETRY_AFTER                  # waiting to retry a failed attempt
    return slot + timedelta(days=1)


def launchd_time() -> str | None:
    """The macOS launchd schedule (`jobagent schedule`), if one is installed: "HH:MM"."""
    import plistlib
    from pathlib import Path
    plist = Path.home() / "Library/LaunchAgents/com.fazil.jobagent.plist"
    try:
        with open(plist, "rb") as f:
            cal = plistlib.load(f).get("StartCalendarInterval") or {}
        return f"{int(cal.get('Hour', 0)):02d}:{int(cal.get('Minute', 0)):02d}"
    except Exception:
        return None


def last_run_start(db_path) -> datetime | None:
    from . import db
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT MAX(started_at) AS s FROM runs").fetchone()
    finally:
        conn.close()
    if not row or not row["s"]:
        return None
    return datetime.fromisoformat(row["s"]).astimezone()


def start(at: time, db_path, launch: Callable[[], bool], check_every: float = 60) -> threading.Thread:
    """Start the background loop. `launch()` starts a run and returns False if one is going."""
    stop = threading.Event()
    STATE.update(enabled=True, at=at)

    def loop():
        last_attempt = None
        log.info("daily run scheduled at %s (%s time)", at.strftime("%H:%M"),
                 os.environ.get("TZ") or datetime.now().astimezone().tzname())
        while not stop.is_set():
            try:
                now = datetime.now().astimezone()
                STATE["last_check"] = now
                if is_due(now, at, last_run_start(db_path), last_attempt):
                    last_attempt = now
                    STATE["last_attempt"] = now
                    log.info("starting the daily run")
                    launch()
            except Exception:                      # never let the scheduler thread die
                log.exception("daily scheduler check failed")
            stop.wait(check_every)

    t = threading.Thread(target=loop, name="daily-run", daemon=True)
    t.stop = stop                                   # type: ignore[attr-defined]
    t.start()
    return t
