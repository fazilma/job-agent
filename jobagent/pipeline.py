"""Orchestrator: the daily run. Plain code; only scoring and tailoring call Claude."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import collectors, db, enrich, parallel, rules, scorer, tailor, timeouts
from .models import ACTIVE, FILTERED, RawJob
from .normalize import fingerprint

log = logging.getLogger("jobagent.pipeline")


def _prune_raw(raw_dir: Path, days: int) -> None:
    if not raw_dir.exists():
        return
    cutoff = time.time() - days * 86400
    for p in raw_dir.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def ingest(conn, cfg, jobs: list[RawJob], run_id: int) -> dict:
    """Dedupe + filter + store. Existing jobs keep their status (append-only)."""
    rule_cfg = cfg.section("filter", default={})
    stats = {"seen": len(jobs), "new": 0, "new_filtered": 0, "merged": 0, "revived": 0}
    for raw in jobs:
        if not raw.title or not raw.company:
            continue
        fp = fingerprint(raw.company, raw.title, raw.location, raw.remote)
        keep, reason = rules.check(raw, rule_cfg)
        status = ACTIVE if keep else FILTERED
        job_id, is_new = db.upsert_job(conn, raw, fp, run_id, status=status,
                                       filter_reason=None if keep else reason)
        if is_new:
            stats["new" if keep else "new_filtered"] += 1
            continue
        stats["merged"] += 1
        # Rules changed since we first saw it? Give it another chance.
        row = db.get_job(conn, job_id)
        if row["status"] == FILTERED and keep:
            db.set_status(conn, job_id, ACTIVE, "now passes filter")
            stats["revived"] += 1
    conn.commit()
    return stats


def refilter(conn, cfg) -> dict:
    """Re-check Active and filtered-out jobs against the current rules.

    Active jobs that now fail go to filtered; filtered jobs that now pass come back
    to Active (and get scored on the next run). Opened/applied/skipped/closed jobs
    are never touched, so editing the filter in config.yaml never loses your actions.
    """
    rule_cfg = cfg.section("filter", default={})
    stats = {"checked": 0, "dropped": 0, "revived": 0}
    rows = conn.execute(f"SELECT * FROM jobs WHERE status IN ('{ACTIVE}','{FILTERED}')").fetchall()
    for r in rows:
        stats["checked"] += 1
        raw = RawJob(source=r["primary_source"] or "", title=r["title"], company=r["company"],
                     location=r["location"] or "", description=r["description"] or "", remote=bool(r["remote"]))
        keep, reason = rules.check(raw, rule_cfg)
        if r["status"] == ACTIVE and not keep:
            conn.execute("UPDATE jobs SET filter_reason=? WHERE id=?", (reason, r["id"]))
            db.set_status(conn, r["id"], FILTERED, f"refilter: {reason}")
            stats["dropped"] += 1
        elif r["status"] == FILTERED:
            if keep:
                conn.execute("UPDATE jobs SET filter_reason=NULL WHERE id=?", (r["id"],))
                db.set_status(conn, r["id"], ACTIVE, "refilter: now passes rules")
                stats["revived"] += 1
            elif reason != r["filter_reason"]:
                conn.execute("UPDATE jobs SET filter_reason=? WHERE id=?", (reason, r["id"]))
    stats["gigs"] = mark_gigs(conn)
    conn.commit()
    return stats


def mark_gigs(conn) -> int:
    """Tag AI-training gigs by keyword rules; returns how many jobs are tagged now.
    A tag the scorer added (gig=2) is kept; a rule tag is removed if the rules stop matching."""
    for r in conn.execute("SELECT id, title, description, gig FROM jobs WHERE gig IN (0, 1)").fetchall():
        want = 1 if rules.gig_reason(r["title"], r["description"]) else 0
        if want != r["gig"]:
            conn.execute("UPDATE jobs SET gig=? WHERE id=?", (want, r["id"]))
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE gig > 0").fetchone()[0]


class NoNetwork(RuntimeError):
    pass


def wait_for_network(max_wait_s: float = 120, hosts=("api.apify.com", "boards-api.greenhouse.io")) -> bool:
    """True once a DNS lookup works. After sleep, Wi-Fi often takes a minute to come back."""
    import socket
    deadline = time.time() + max_wait_s
    while True:
        for h in hosts:
            try:
                socket.getaddrinfo(h, 443)
                return True
            except OSError:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(10)


def run(cfg, *, sources: list[str] | None = None, skip_score: bool = False,
        skip_tailor: bool = False, scorer_llm=None, tailor_llm=None,
        collector_overrides: dict | None = None) -> dict:
    if collector_overrides is None and cfg.section("run", "require_network", default=True):
        if not wait_for_network(float(cfg.section("run", "network_wait_seconds", default=120))):
            log.error("No internet connection (DNS lookups failing). Run skipped; nothing changed.")
            raise NoNetwork("no internet connection")
    conn = db.connect(cfg.path("db"))
    raw_dir = cfg.path("raw_dir")
    run_id = db.start_run(conn)
    stats: dict = {"run_id": run_id, "sources": {}}
    wall0, mono0 = time.time(), time.monotonic()
    try:
        active = collector_overrides or collectors.enabled(cfg)
        if sources:
            active = {k: v for k, v in active.items() if k in sources}
        jobs: list[RawJob] = []
        # Circuit breaker: a source that keeps failing is skipped for a while instead of
        # costing time (and Apify money) every run.
        ts = timeouts.settings(cfg)
        for name in list(active):
            why = timeouts.breaker(conn, name, ts["failures"], ts["cooldown_hours"])
            if why and collector_overrides is None:
                log.warning("skipping %s: %s", name, why)
                stats["sources"][name] = {"skipped": why}
                active.pop(name)
        # Sources are independent (different sites), so collect them side by side. Each gets
        # its own deadline: a few times its usual (median) time, so a stuck source is cut off
        # soon instead of holding up the whole run.
        deadlines = {name: timeouts.source_timeout(conn, cfg, name) for name in active}
        stats["timeouts"] = deadlines
        t_collect = time.monotonic()
        results = parallel.run_all({name: (lambda fn=fn: fn(cfg, raw_dir)) for name, fn in active.items()},
                                   max_workers=max(1, len(active)), deadline_s=deadlines)
        for name, (res, secs) in results.items():
            if isinstance(res, BaseException):           # one broken source never kills the run
                log.warning("collector %s failed after %.0fs: %s", name, secs, res)
                stats["sources"][name] = {"error": str(res)[:300], "secs": round(secs, 1)}
            else:
                stats["sources"][name] = {"jobs": len(res), "secs": round(secs, 1)}
                jobs.extend(res)
        stats["collect_secs"] = round(time.monotonic() - t_collect, 1)
        log.info("collected %d jobs from %d sources in %.0fs", len(jobs), len(active), stats["collect_secs"])

        stats["refilter"] = refilter(conn, cfg)            # rules may have changed since last run
        stats.update(ingest(conn, cfg, jobs, run_id))
        stats["gigs"] = mark_gigs(conn)                    # before scoring/tailoring sees the new jobs
        conn.commit()
        if collector_overrides is None:                    # real runs: look for more company boards
            try:
                from .collectors import discover
                stats["boards"] = discover.discover(conn, cfg)
            except Exception as e:
                log.warning("career-board discovery failed: %s", e)
        stats["auto_closed"] = db.auto_close(conn, cfg.section("tracking", "close_after_unseen_days", default=14))

        if collector_overrides is None:                    # real runs only (tests use fake URLs)
            try:
                stats["enriched"] = enrich.fill_short_descriptions(conn, cfg)   # before scoring
            except Exception as e:
                log.warning("enrich step failed: %s", e)
        if not skip_score:
            t0 = time.monotonic()
            try:
                stats["scored"] = scorer.score_pending(conn, cfg, llm=scorer_llm)
                stats["score_secs"] = round(time.monotonic() - t0, 1)
            except Exception as e:
                log.exception("scoring failed")
                stats["score_error"] = str(e)[:300]
        if not skip_tailor:
            t0 = time.monotonic()
            try:
                stats["tailored"] = tailor.tailor_top(conn, cfg, llm=tailor_llm)
                stats["tailor_secs"] = round(time.monotonic() - t0, 1)
            except Exception as e:
                log.exception("tailoring failed")
                stats["tailor_error"] = str(e)[:300]
        _prune_raw(raw_dir, cfg.section("tracking", "raw_retention_days", default=14))
    finally:
        # Wall clock minus the monotonic clock = time the machine was asleep during the run.
        slept = (time.time() - wall0) - (time.monotonic() - mono0)
        if slept > 60:
            stats["slept_secs"] = round(slept)
            log.warning("the computer was asleep for about %.0f min during this run", slept / 60)
        db.finish_run(conn, run_id, stats)
        conn.close()
    return stats
