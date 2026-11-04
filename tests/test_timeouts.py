import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from jobagent import db, parallel, pipeline, timeouts
from jobagent.config import Config, load_config


@pytest.fixture
def cfg(tmp_path):
    c = Config(json.loads(json.dumps(load_config())))
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(tmp_path), "base_resume": str(tmp_path / "r.md")}
    c["run"] = {"collect_timeout_minutes": 15, "timeout_factor": 3,
                "circuit_breaker_failures": 3, "circuit_breaker_hours": 48}
    return c


def add_run(conn, started: datetime, sources: dict):
    rid = conn.execute("INSERT INTO runs(started_at) VALUES (?)", (started.isoformat(),)).lastrowid
    conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?",
                 (started.isoformat(), json.dumps({"sources": sources}), rid))
    conn.commit()


def test_adaptive_uses_median_and_clamps():
    assert timeouts.adaptive([10, 12, 11], 3, 60, 900, 900) == 60            # floor
    assert timeouts.adaptive([40, 50, 45, 3000], 3, 60, 900, 900) == 142.5   # one huge outlier ignored
    assert timeouts.adaptive([400, 500, 450], 3, 60, 900, 900) == 900        # ceiling
    assert timeouts.adaptive([10], 3, 60, 900, 777) == 777                   # too little history


def test_source_timeout_from_history(cfg):
    conn = db.connect(cfg.path("db"))
    now = datetime.now(timezone.utc)
    for i, secs in enumerate([50, 55, 60, 5000]):                           # 5000 = old reading incl. sleep
        add_run(conn, now - timedelta(days=i), {"agencies": {"jobs": 5, "secs": secs},
                                                 "linkedin": {"jobs": 9, "secs": 50}})
    assert timeouts.source_timeout(conn, cfg, "agencies") == 165             # 3 x median(50, 55, 60)
    assert timeouts.source_timeout(conn, cfg, "linkedin") == 600             # Apify floor, not 150
    assert timeouts.source_timeout(conn, cfg, "brand_new") == 900            # no history: the cap


def test_breaker_opens_after_three_failures_then_retries(cfg):
    conn = db.connect(cfg.path("db"))
    now = datetime.now(timezone.utc)
    add_run(conn, now - timedelta(days=4), {"nodesk": {"jobs": 3, "secs": 1}})
    for d in (3, 2, 1):
        add_run(conn, now - timedelta(days=d, hours=-1), {"nodesk": {"error": "bad xml", "secs": 1}})
    why = timeouts.breaker(conn, "nodesk", 3, 48, now=now)
    assert why and "failed 3 runs in a row" in why and "bad xml" in why
    # a skipped run is not an attempt and doesn't reset anything
    add_run(conn, now - timedelta(hours=1), {"nodesk": {"skipped": why}})
    assert timeouts.breaker(conn, "nodesk", 3, 48, now=now)
    # after the cooldown: half-open, try once
    assert timeouts.breaker(conn, "nodesk", 3, 48, now=now + timedelta(days=3)) is None
    # one success closes it
    add_run(conn, now, {"nodesk": {"jobs": 2, "secs": 1}})
    assert timeouts.breaker(conn, "nodesk", 3, 48, now=now) is None


def test_run_skips_open_circuit_and_records_timeouts(cfg, monkeypatch):
    conn = db.connect(cfg.path("db"))
    now = datetime.now(timezone.utc)
    for d in (3, 2, 1):
        add_run(conn, now - timedelta(days=d), {"broken": {"error": "boom", "secs": 1}})
    called = []
    sources = {"broken": lambda c, r: called.append("broken") or [], "fine": lambda c, r: []}
    monkeypatch.setattr(pipeline.collectors, "enabled", lambda c: dict(sources))
    monkeypatch.setattr(pipeline, "wait_for_network", lambda *a, **k: True)       # real-run path, no network
    stats = pipeline.run(cfg, skip_score=True, skip_tailor=True)
    assert called == [] and "circuit open" in stats["sources"]["broken"]["skipped"]
    assert stats["sources"]["fine"] == {"jobs": 0, "secs": stats["sources"]["fine"]["secs"]}
    assert stats["timeouts"]["fine"] == 900


def test_per_task_deadlines():
    gate = threading.Event()
    res = parallel.run_all({"fast": lambda: "ok", "stuck": lambda: gate.wait(5)},
                           max_workers=2, deadline_s={"fast": 2, "stuck": 0.2})
    gate.set()
    assert res["fast"][0] == "ok"
    assert isinstance(res["stuck"][0], parallel.Abandoned) and "0s" in str(res["stuck"][0])


def test_claude_timeouts(cfg):
    conn = db.connect(cfg.path("db"))
    for secs in (20, 25, 30):
        db.log_claude_usage(conn, "score", {"secs": secs, "ok": True})
    for secs in (100, 150, 200, 9000):                                       # 9000: slept, ignored
        db.log_claude_usage(conn, "tailor", {"secs": secs, "ok": True})
    assert timeouts.score_timeout(conn, cfg) == 120                          # floor
    assert timeouts.tailor_timeout(conn, cfg) == 900                         # floor, not 450
