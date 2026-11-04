from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Iterable

from .models import (ACTIVE, APPLIED, CLOSED, DIRECT_SOURCES, FILTERED,
                     INTERVIEWING, OPENED, SKIPPED, RawJob)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    stats        TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    remote          INTEGER DEFAULT 0,
    url             TEXT,
    apply_url       TEXT,
    description     TEXT,
    salary          TEXT,
    posted_at       TEXT,
    sources         TEXT NOT NULL DEFAULT '[]',
    primary_source  TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    first_run_id    INTEGER,
    status          TEXT NOT NULL DEFAULT 'active',
    status_changed_at TEXT,
    filter_reason   TEXT,
    score           INTEGER,
    score_reasons   TEXT,
    red_flags       TEXT,
    scored_at       TEXT,
    tailor_status   TEXT NOT NULL DEFAULT 'none',   -- none|queued|running|done|failed
    tailor_error    TEXT,
    resume_dir      TEXT,
    resume_pdf      TEXT,
    answers         TEXT,
    applied_at      TEXT,
    skip_reason     TEXT,
    notes           TEXT,
    gig             INTEGER NOT NULL DEFAULT 0   -- AI-training gig: 0 no, 1 by rule, 2 by the scorer
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);

CREATE TABLE IF NOT EXISTS claude_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    purpose     TEXT NOT NULL,          -- score | tailor
    job_id      INTEGER,
    model       TEXT,
    ok          INTEGER,
    cost_usd    REAL,                   -- Claude Code's estimate at API list price
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    turns       INTEGER,
    secs        REAL
);

CREATE TABLE IF NOT EXISTS career_boards (    -- company boards found by collectors/discover.py
    company_key TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    ats         TEXT,                     -- greenhouse | lever | ashby, NULL when none was found
    token       TEXT,
    status      TEXT NOT NULL,            -- found | none
    uae_jobs    INTEGER,
    checked_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    INTEGER NOT NULL REFERENCES jobs(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason    TEXT,
    at        TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release; older databases get them on first connect.
_ADDED_COLUMNS = {"jobs": [("gig", "INTEGER NOT NULL DEFAULT 0")]}


# Bump when normalize.fingerprint changes, so stored jobs are re-keyed once and a changed
# rule doesn't make the next run add every existing job again as "new".
FINGERPRINT_VERSION = 2


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key='fingerprint_version'").fetchone()
    if row is None or int(row[0]) < FINGERPRINT_VERSION:
        refingerprint(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('fingerprint_version', ?)",
                     (str(FINGERPRINT_VERSION),))
    conn.commit()


def refingerprint(conn: sqlite3.Connection) -> int:
    """Recompute every job's fingerprint with the current rules. Returns how many changed.
    If a job's new key is already taken (a duplicate the older rules missed, e.g.
    "Engineer, Full-Stack" next to "Engineer (Full-Stack)"), it keeps its old key, so both rows stay."""
    from .normalize import fingerprint
    changed = 0
    for r in conn.execute("SELECT id, company, title, location, remote, fingerprint FROM jobs").fetchall():
        fp = fingerprint(r["company"], r["title"], r["location"] or "", bool(r["remote"]))
        if fp == r["fingerprint"]:
            continue
        try:
            conn.execute("UPDATE jobs SET fingerprint=? WHERE id=?", (fp, r["id"]))
            changed += 1
        except sqlite3.IntegrityError:
            pass
    return changed


# ------------------------------------------------------------------ runs

def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs(started_at) VALUES (?)", (now(),))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, stats: dict) -> None:
    conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?",
                 (now(), json.dumps(stats), run_id))
    conn.commit()


def latest_run(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()


# ------------------------------------------------------------------ jobs

PREFERRED_SOURCES = ["linkedin"]        # after the company's own career page, before the rest


def source_rank(source: str | None) -> int:
    """Lower is better: 0 = company career page, 1.. = PREFERRED_SOURCES in order, then the rest."""
    if source in DIRECT_SOURCES:
        return 0
    if source in PREFERRED_SOURCES:
        return 1 + PREFERRED_SOURCES.index(source)
    return 1 + len(PREFERRED_SOURCES)


def upsert_job(conn: sqlite3.Connection, raw: RawJob, fingerprint: str, run_id: int,
               status: str = ACTIVE, filter_reason: str | None = None) -> tuple[int, bool]:
    """Insert a new job or merge into the existing one. Returns (id, is_new).

    Existing jobs keep their status: a job you skipped or applied to stays
    there even though the board shows it again.
    """
    ts = now()
    row = conn.execute("SELECT * FROM jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row is None and raw.url and urlparse(raw.url).path.strip("/"):
        # Same posting URL = same job, even if title/location text differs between
        # a listing page and its detail page (common on recruiter sites).
        row = conn.execute("SELECT * FROM jobs WHERE url=? ORDER BY id LIMIT 1", (raw.url,)).fetchone()
    if row is None:
        cur = conn.execute(
            """INSERT INTO jobs(fingerprint, company, title, location, remote, url, apply_url,
                   description, salary, posted_at, sources, primary_source, first_seen, last_seen,
                   first_run_id, status, status_changed_at, filter_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, raw.company, raw.title, raw.location, int(raw.remote), raw.url,
             raw.apply_url or raw.url, raw.description, raw.salary, raw.posted_at,
             json.dumps([raw.source]), raw.source, ts, ts, run_id, status, ts, filter_reason))
        job_id = cur.lastrowid
        conn.execute("INSERT INTO status_history(job_id, from_status, to_status, reason, at) VALUES (?,?,?,?,?)",
                     (job_id, None, status, filter_reason or "discovered", ts))
        return job_id, True

    sources = json.loads(row["sources"] or "[]")
    if raw.source not in sources:
        sources.append(raw.source)
    updates = {"last_seen": ts, "sources": json.dumps(sources)}
    # Which copy the dashboard shows: the company's own career page, then LinkedIn, then
    # everything else (Bayt, NaukriGulf, agencies, remote boards).
    if source_rank(raw.source) < source_rank(row["primary_source"]):
        updates.update(primary_source=raw.source, apply_url=raw.apply_url or raw.url)
        if raw.source not in DIRECT_SOURCES and raw.url:
            updates["url"] = raw.url            # e.g. the LinkedIn posting instead of Bayt's
    # A page from the open web can copy a job's name, so its text never replaces what we have.
    if raw.source != "web_search" and raw.description and len(raw.description) > len(row["description"] or ""):
        updates["description"] = raw.description
    for col in ("salary", "posted_at", "location"):
        if not row[col] and getattr(raw, col):
            updates[col] = getattr(raw, col)
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*updates.values(), row["id"]))
    return row["id"], False


def get_job(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def set_status(conn: sqlite3.Connection, job_id: int, status: str, reason: str | None = None) -> None:
    row = get_job(conn, job_id)
    if row is None:
        raise KeyError(job_id)
    ts = now()
    extra = {}
    if status == APPLIED and not row["applied_at"]:
        extra["applied_at"] = ts
    if status == SKIPPED:
        extra["skip_reason"] = reason
    sets = ", ".join(["status=?", "status_changed_at=?"] + [f"{k}=?" for k in extra])
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (status, ts, *extra.values(), job_id))
    conn.execute("INSERT INTO status_history(job_id, from_status, to_status, reason, at) VALUES (?,?,?,?,?)",
                 (job_id, row["status"], status, reason, ts))
    conn.commit()


def set_notes(conn: sqlite3.Connection, job_id: int, notes: str) -> None:
    conn.execute("UPDATE jobs SET notes=? WHERE id=?", (notes, job_id))
    conn.commit()


def history(conn: sqlite3.Connection, job_id: int):
    return conn.execute("SELECT * FROM status_history WHERE job_id=? ORDER BY id", (job_id,)).fetchall()


# ------------------------------------------------------------------ scoring / tailoring

def jobs_needing_score(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        f"""SELECT * FROM jobs WHERE score IS NULL AND status IN ('{ACTIVE}','{OPENED}')
            ORDER BY first_seen DESC LIMIT ?""", (limit,)).fetchall()


def set_score(conn: sqlite3.Connection, job_id: int, score: int, reasons: list[str], red_flags: list[str],
              gig: bool = False) -> None:
    conn.execute("UPDATE jobs SET score=?, score_reasons=?, red_flags=?, scored_at=? WHERE id=?",
                 (score, json.dumps(reasons), json.dumps(red_flags), now(), job_id))
    if gig:                                    # the scorer spotted a gig the keyword rules missed
        conn.execute("UPDATE jobs SET gig=2 WHERE id=? AND gig=0", (job_id,))


def jobs_to_tailor(conn: sqlite3.Connection, threshold: int, limit: int):
    """Queued-by-hand first, then the best automatic matches. Text from the open web never
    runs unattended: tailoring has file and network tools, and a posting could carry instructions."""
    return conn.execute(
        f"""SELECT * FROM jobs
            WHERE status IN ('{ACTIVE}','{OPENED}')
              AND (tailor_status='queued' OR (tailor_status='none' AND gig=0 AND score >= ?
                                              AND COALESCE(sources,'') NOT LIKE '%"web_search"%'))
            ORDER BY (tailor_status='queued') DESC, score DESC LIMIT ?""",
        (threshold, limit)).fetchall()


def set_tailor(conn: sqlite3.Connection, job_id: int, status: str, *, resume_dir: str | None = None,
               resume_pdf: str | None = None, answers: str | None = None, error: str | None = None) -> None:
    conn.execute(
        """UPDATE jobs SET tailor_status=?, resume_dir=COALESCE(?, resume_dir),
               resume_pdf=COALESCE(?, resume_pdf), answers=COALESCE(?, answers), tailor_error=?
           WHERE id=?""", (status, resume_dir, resume_pdf, answers, error, job_id))
    conn.commit()


# Skips that say the posting is gone, not that Fazil dislikes the job: the scorer ignores these.
NOT_AVAILABLE = re.compile(r"no longer (available|open|accepting)|not available|expired|closed|filled|"
                           r"removed|taken down|job not found|no longer exists", re.I)


def skip_feedback(conn: sqlite3.Connection, limit: int = 25) -> list[str]:
    """Recent skip reasons, fed to the scorer as 'things Fazil doesn't want'. Skips because
    the job is no longer available are left out: they say nothing about what he wants."""
    rows = conn.execute(
        f"""SELECT title, company, skip_reason FROM jobs
            WHERE status='{SKIPPED}' AND skip_reason IS NOT NULL AND skip_reason != ''
            ORDER BY status_changed_at DESC LIMIT ?""", (limit * 3,)).fetchall()
    rows = [r for r in rows if not NOT_AVAILABLE.search(r["skip_reason"])][:limit]
    return [f"{r['title']} @ {r['company']}: {r['skip_reason']}" for r in rows]


# ------------------------------------------------------------------ housekeeping

def auto_close(conn: sqlite3.Connection, unseen_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=unseen_days)).replace(microsecond=0).isoformat()
    rows = conn.execute(
        f"SELECT id FROM jobs WHERE status IN ('{ACTIVE}','{OPENED}') AND last_seen < ?", (cutoff,)).fetchall()
    for r in rows:
        set_status(conn, r["id"], CLOSED, f"not seen for {unseen_days} days")
    return len(rows)


# ------------------------------------------------------------------ dashboard queries

TAB_QUERIES = {
    "new": f"status IN ('{ACTIVE}','{OPENED}') AND first_run_id = :latest_run",
    "active": f"status IN ('{ACTIVE}','{OPENED}')",
    "applied": f"status IN ('{APPLIED}','{INTERVIEWING}')",
    "skipped": f"status = '{SKIPPED}'",
    "closed": f"status = '{CLOSED}'",
    "all": f"status != '{FILTERED}'",
}

TAB_ORDER = {
    "applied": "applied_at DESC",
    "skipped": "status_changed_at DESC",
    "closed": "status_changed_at DESC",
}


RESUME_READY = "tailor_status = 'done' AND resume_pdf IS NOT NULL"


def list_tab(conn: sqlite3.Connection, tab: str, q: str = "", min_score: int | None = None,
             include_filtered: bool = False, resume_ready: bool = False, gigs_only: bool = False):
    run = conn.execute("SELECT MAX(id) AS r FROM runs WHERE finished_at IS NOT NULL").fetchone()
    where = ["1=1" if tab == "all" and include_filtered else TAB_QUERIES[tab]]
    params: dict = {"latest_run": run["r"] if run else None}
    if q:
        where.append("(title LIKE :q OR company LIKE :q OR location LIKE :q)")
        params["q"] = f"%{q}%"
    if resume_ready:
        where.append(RESUME_READY)
    if gigs_only:
        where.append("gig > 0")
    if min_score is not None:
        where.append("COALESCE(score, 0) >= :min_score")
        params["min_score"] = min_score
        if not gigs_only:
            where.append("gig = 0")             # "strong matches" means real engineering roles
    order = TAB_ORDER.get(tab, "COALESCE(score, -1) DESC, first_seen DESC")
    return conn.execute(f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY {order}", params).fetchall()


def tab_counts(conn: sqlite3.Connection) -> dict[str, int]:
    run = conn.execute("SELECT MAX(id) AS r FROM runs WHERE finished_at IS NOT NULL").fetchone()
    params = {"latest_run": run["r"] if run else None}
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {w}", params).fetchone()[0]
              for t, w in TAB_QUERIES.items()}
    counts["resume_ready"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {RESUME_READY} AND status IN ('{ACTIVE}','{OPENED}')").fetchone()[0]
    counts["filtered"] = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE status='{FILTERED}'").fetchone()[0]
    counts["gigs"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE gig > 0 AND status IN ('{ACTIVE}','{OPENED}')").fetchone()[0]
    return counts


def pending_confirmations(conn: sqlite3.Connection):
    """Jobs where you clicked Apply but never confirmed."""
    return conn.execute(f"SELECT * FROM jobs WHERE status='{OPENED}' ORDER BY status_changed_at").fetchall()


def follow_ups(conn: sqlite3.Connection, days: int) -> set[int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = conn.execute(f"SELECT id FROM jobs WHERE status='{APPLIED}' AND applied_at < ?", (cutoff,)).fetchall()
    return {r["id"] for r in rows}


def bulk(conn: sqlite3.Connection, rows: Iterable) -> list[dict]:
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ Claude usage

def log_claude_usage(conn: sqlite3.Connection, purpose: str, usage: dict, job_id: int | None = None) -> None:
    if not usage:
        return
    conn.execute(
        """INSERT INTO claude_usage(at, purpose, job_id, model, ok, cost_usd, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, turns, secs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), purpose, job_id, usage.get("model"), int(bool(usage.get("ok", True))), usage.get("cost_usd", 0.0),
         usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cache_read_tokens", 0),
         usage.get("cache_write_tokens", 0), usage.get("turns", 0), usage.get("secs", 0.0)))
    conn.commit()


def claude_usage_by_day(conn: sqlite3.Connection, days: int = 7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    return conn.execute(
        """SELECT substr(at, 1, 10) AS day, purpose, COALESCE(NULLIF(model, ''), '?') AS model,
                  COUNT(*) AS calls, SUM(cost_usd) AS cost,
                  SUM(input_tokens + cache_read_tokens + cache_write_tokens) AS tokens_in,
                  SUM(output_tokens) AS tokens_out, SUM(turns) AS turns, SUM(secs) AS secs
           FROM claude_usage WHERE at >= ? GROUP BY day, purpose, model
           ORDER BY day, purpose, model""", (cutoff,)).fetchall()


def costliest_tailorings(conn: sqlite3.Connection, days: int = 7, limit: int = 5):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    return conn.execute(
        """SELECT u.at, u.cost_usd, u.turns, u.secs, u.model, j.company, j.title FROM claude_usage u
           LEFT JOIN jobs j ON j.id = u.job_id
           WHERE u.purpose = 'tailor' AND u.at >= ? ORDER BY u.cost_usd DESC LIMIT ?""", (cutoff, limit)).fetchall()
