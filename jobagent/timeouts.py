"""Adaptive timeouts and a circuit breaker, learned from past runs.

Timeout: each step (a source, a scoring call, a tailoring) may take `timeout_factor` times its
usual time, where "usual" is the median of its recent successful runs. With too little history
a configured default is used. Timings use the monotonic clock, which stops while the Mac sleeps,
so a sleeping Mac neither cuts work off early nor inflates the history.

Circuit breaker: a source that failed its last `failures` attempts in a row is skipped
("circuit open") until `cooldown_hours` have passed since its last attempt; then it's tried
once more ("half-open"). One success closes the circuit again.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone


def adaptive(samples: list[float], factor: float, floor: float, ceiling: float, default: float,
             min_samples: int = 3) -> float:
    """factor x median of past successes, kept within [floor, ceiling]."""
    if len(samples) < min_samples:
        return default
    return round(max(floor, min(ceiling, factor * statistics.median(samples))), 1)


def _runs(conn, limit: int = 30):
    out = []
    for r in conn.execute("SELECT started_at, stats FROM runs WHERE finished_at IS NOT NULL "
                          "ORDER BY id DESC LIMIT ?", (limit,)):
        try:
            out.append((r["started_at"], json.loads(r["stats"] or "{}")))
        except json.JSONDecodeError:
            continue
    return out


def source_samples(conn, name: str, cap: float, n: int = 10) -> list[float]:
    """Durations of the source's recent successful collections (newest first).
    Anything longer than `cap` can only be an old wall-clock reading that included sleep
    (the source would have been cut off), so it's left out."""
    out = []
    for _, st in _runs(conn):
        s = (st.get("sources") or {}).get(name) or {}
        if "jobs" in s and s.get("secs") is not None and s["secs"] <= cap:
            out.append(float(s["secs"]))
            if len(out) >= n:
                break
    return out


def breaker(conn, name: str, failures: int = 3, cooldown_hours: float = 48,
            now: datetime | None = None) -> str | None:
    """Why the source should be skipped this run, or None to run it."""
    now = now or datetime.now(timezone.utc)
    streak, last_attempt, last_error = 0, None, ""
    for started, st in _runs(conn):
        s = (st.get("sources") or {}).get(name)
        if not s or s.get("skipped"):              # not run, or skipped by the breaker: not an attempt
            continue
        if last_attempt is None:
            last_attempt = datetime.fromisoformat(started)
        if "error" not in s:
            break
        streak += 1
        last_error = last_error or s["error"]
        if streak >= failures:
            break
    if streak < failures or last_attempt is None:
        return None
    retry_at = last_attempt + timedelta(hours=cooldown_hours)
    if now >= retry_at:
        return None                                 # half-open: give it one more try
    return (f"circuit open: failed {streak} runs in a row (last: {last_error[:80]}); "
            f"next try after {retry_at.astimezone():%a %d %b %H:%M}")


def claude_samples(conn, purpose: str, cap: float, n: int = 20) -> list[float]:
    rows = conn.execute("SELECT secs FROM claude_usage WHERE purpose=? AND ok=1 AND secs IS NOT NULL "
                        "AND secs <= ? ORDER BY id DESC LIMIT ?", (purpose, cap, n)).fetchall()
    return [float(r["secs"]) for r in rows]


def settings(cfg) -> dict:
    r = cfg.section("run", default={}) or {}
    return {"factor": float(r.get("timeout_factor", 3)),
            "failures": int(r.get("circuit_breaker_failures", 3)),
            "cooldown_hours": float(r.get("circuit_breaker_hours", 48)),
            "collect_cap": float(r.get("collect_timeout_minutes", 15)) * 60}


# Sources that start paid Apify searches: cutting them off early doesn't stop the searches on
# Apify (you'd pay and lose the results), so they always get the time Apify itself allows.
APIFY_SOURCES = {"linkedin", "bayt", "naukrigulf"}


def source_floor(cfg, name: str) -> float:
    if name == "linkedin_public":                      # spaced-out requests plus a possible 60s rate-limit pause
        return 600
    if name == "career_pages":                         # grows as discovery finds more company boards
        return 300
    if name in APIFY_SOURCES:
        per_search = float(cfg.section("apify", "max_seconds_per_search", default=240))
        return per_search * 2 + 120                     # same as the per-search deadline inside
    return 60


def source_timeout(conn, cfg, name: str) -> float:
    s = settings(cfg)
    floor = min(source_floor(cfg, name), s["collect_cap"])
    return adaptive(source_samples(conn, name, s["collect_cap"]), s["factor"],
                    floor=floor, ceiling=s["collect_cap"], default=s["collect_cap"])


def score_timeout(conn, cfg) -> float:
    s = settings(cfg)
    return adaptive(claude_samples(conn, "score", cap=600), s["factor"], floor=120, ceiling=600, default=300)


def tailor_timeout(conn, cfg) -> float:
    """Tailorings vary a lot (2 to 15 minutes) and a cut-off one still costs its tokens,
    so the floor is generous: this is for a session that is truly stuck."""
    s = settings(cfg)
    cap = float(cfg.section("tailor", "timeout_minutes", default=25)) * 60
    return adaptive(claude_samples(conn, "tailor", cap=cap), s["factor"], floor=min(900, cap),
                    ceiling=cap, default=cap)
