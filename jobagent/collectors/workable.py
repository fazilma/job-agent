"""Workable, searched across every company that uses it ("Jobs by Workable").

Workable hosts the career pages of many UAE startups and mid-size companies. Its public job
site (jobs.workable.com) searches all of them at once, by keyword and location, through a JSON
API the site itself uses. No login, no key. Each result carries the full description,
requirements and benefits, so no second request per job is needed.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..models import RawJob
from .base import html_to_text, http_client, save_raw, to_iso

log = logging.getLogger("jobagent.collect.workable")

API = "https://jobs.workable.com/api/v1/jobs"


def to_raw(j: dict) -> RawJob:
    locs = [("Remote" if l == "TELECOMMUTE" else l) for l in (j.get("locations") or []) if l]
    workplace = (j.get("workplace") or "").lower()
    location = " / ".join(dict.fromkeys(locs))
    if workplace == "remote" and "remote" not in location.lower():
        location = f"Remote ({location})" if location else "Remote"
    parts = [html_to_text(j.get(k)) for k in ("description", "requirementsSection", "benefitsSection")]
    company = (j.get("company") or {}).get("title") or ""
    return RawJob(source="workable", external_id=str(j.get("id", "")), title=(j.get("title") or "").strip(),
                  company=company.strip(), location=location, url=j.get("url", ""), apply_url=j.get("url", ""),
                  description="\n\n".join(p for p in parts if p), remote=workplace == "remote" or "Remote" in locs,
                  posted_at=to_iso(j.get("created")),
                  extra={"workplace": workplace, "employment": j.get("employmentType", "")})


def collect(cfg, raw_dir: Path | None = None, client=None, sleep=time.sleep) -> list[RawJob]:
    c = cfg.section("sources", "workable", default={}) or {}
    queries = c.get("queries") or []
    location = c.get("location", "United Arab Emirates")
    max_per = int(c.get("max_per_query", 100))
    day_range = c.get("day_range", 30)
    own = client is None
    client = client or http_client()
    jobs: dict[str, RawJob] = {}
    raw: list[dict] = []
    try:
        for q in queries:
            token, got = None, 0
            while got < max_per:
                params = {"query": q, "location": location}
                if day_range:
                    params["day_range"] = day_range
                if token:
                    params["pageToken"] = token
                r = client.get(API, params=params)
                r.raise_for_status()
                data = r.json()
                page = data.get("jobs") or []
                raw.extend(page)
                for j in page:
                    job = to_raw(j)
                    if job.title and job.company:
                        jobs.setdefault(job.external_id or job.url, job)
                got += len(page)
                token = data.get("nextPageToken")
                if not token or not page:
                    break
                sleep(1.0)
            log.info("workable '%s' in %s: %d jobs", q, location, got)
    finally:
        if own:
            client.close()
    save_raw(raw_dir, "workable", raw)
    return list(jobs.values())
