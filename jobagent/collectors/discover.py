"""Find the career boards of UAE employers the agent has already seen.

Greenhouse, Lever and Ashby have no "search all companies", so a company's board can only be
read once its name on that platform (its token) is known. After each run, a few employers
that posted UAE jobs on LinkedIn, Bayt and the other boards are checked: their name is turned
into likely tokens ("Property Finder" -> propertyfinder, property-finder) and each platform is
asked for that board. A board counts only if it currently lists at least one UAE job, so a
same-named company elsewhere isn't picked up. Found boards are read every run, like the
companies in config.yaml; misses are re-checked after a month.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from ..normalize import norm_company, norm_location
from .base import http_client

log = logging.getLogger("jobagent.collect.discover")

PLATFORMS = ("greenhouse", "lever", "ashby")
UAE_PLACES = {"dubai", "abu dhabi", "sharjah", "ajman", "uae"}
# Staffing firms post other companies' jobs; their own boards aren't what we're after.
AGENCY = re.compile(r"recruit|staffing|talent|headhunt|search partners|consult(ing|ancy)? group|"
                    r"\(recruiter\)|manpower|outsourc|placement", re.I)
RECHECK_DAYS = 30


def is_uae(location: str, remote: bool = False) -> bool:
    return norm_location(location or "", remote) in UAE_PLACES or "united arab emirates" in (location or "").lower()


def tokens_for(company: str) -> list[str]:
    """Likely board tokens for a company name, most likely first."""
    raw = re.sub(r"[^a-z0-9 ]+", " ", company.lower().replace("&", " and "))
    words = raw.split()
    core = norm_company(company).split()             # without LLC, Group, Technologies...
    out = []
    for ws in (core, words):
        if ws:
            out += ["".join(ws), "-".join(ws)]
    return [t for t in dict.fromkeys(out) if len(t) >= 3][:4]


def candidates(conn, configured_tokens: set[str], exclude_companies, limit: int) -> list[tuple[str, str]]:
    """(key, company name) of UAE employers worth checking, busiest first."""
    from .. import rules
    seen: dict[str, list] = {}
    for r in conn.execute("""SELECT company, location, remote FROM jobs
                             WHERE primary_source NOT IN ('greenhouse','lever','ashby','smartrecruiters','dover','workable')"""):
        if not is_uae(r["location"], bool(r["remote"])) or AGENCY.search(r["company"] or ""):
            continue
        if rules._has_any(r["company"], exclude_companies):
            continue
        key = norm_company(r["company"])
        if not key:
            continue
        seen.setdefault(key, [r["company"], 0])[1] += 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).isoformat()
    checked = {r["company_key"] for r in conn.execute(
        "SELECT company_key FROM career_boards WHERE status='found' OR checked_at >= ?", (cutoff,))}
    out = [(k, name) for k, (name, n) in sorted(seen.items(), key=lambda kv: -kv[1][1])
           if k not in checked and not (set(tokens_for(name)) & configured_tokens)]
    return out[:limit]


def probe(client, company: str) -> tuple[str, str, int] | None:
    """(platform, token, UAE jobs) for the first board that lists a UAE job, else None."""
    from .ats import FETCHERS
    for token in tokens_for(company):
        for kind in PLATFORMS:
            try:
                jobs = FETCHERS[kind](client, token, company)
            except Exception:
                continue
            uae = sum(1 for j in jobs if is_uae(j.location, j.remote))
            if uae:
                return kind, token, uae
    return None


def discover(conn, cfg, limit: int | None = None, client=None) -> dict:
    """Check a batch of employers; returns {"checked": n, "found": [names]}."""
    from .. import parallel
    d = cfg.section("sources", "discover", default={}) or {}
    if not d.get("enabled", True):
        return {"checked": 0, "found": []}
    limit = limit if limit is not None else int(d.get("per_run", 40))
    configured = {str(c.get("token", "")).lower() for c in cfg.section("sources", "companies", default=[]) or []}
    todo = candidates(conn, configured, (cfg.section("filter", default={}) or {}).get("company_exclude"), limit)
    if not todo:
        return {"checked": 0, "found": []}
    own = client is None
    client = client or http_client()
    try:
        results = parallel.run_all({key: (lambda name=name: probe(client, name)) for key, name in todo},
                                   max_workers=int(cfg.section("run", "parallel_requests", default=8)),
                                   deadline_s=float(d.get("timeout_seconds", 180)))
    finally:
        if own:
            client.close()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    found = []
    for key, name in todo:
        res, _ = results.get(key, (None, 0))
        if isinstance(res, BaseException):
            continue                                   # timed out: try again next run
        kind, token, n = res if res else (None, None, 0)
        conn.execute("""INSERT OR REPLACE INTO career_boards(company_key, company, ats, token, status, uae_jobs, checked_at)
                        VALUES (?,?,?,?,?,?,?)""", (key, name, kind, token, "found" if kind else "none", n, now))
        if kind:
            found.append(f"{name} ({kind})")
            log.info("found a career board for %s: %s/%s with %d UAE jobs", name, kind, token, n)
    conn.commit()
    return {"checked": len(todo), "found": found}


def boards(conn) -> list[dict]:
    """Discovered boards, in the same shape as the companies list in config.yaml."""
    return [{"name": r["company"], "token": r["token"], "ats": r["ats"], "discovered": True}
            for r in conn.execute("SELECT * FROM career_boards WHERE status='found' ORDER BY company")]
