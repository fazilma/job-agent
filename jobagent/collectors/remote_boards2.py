"""Curated remote boards, read through their public APIs and feeds (no browser, no Apify).

  Himalayas        free search API, filtered to jobs open to candidates in the UAE
  Remotive         free API, software-dev category (they ask for at most a few calls a day)
  Working Nomads   free JSON feed of current jobs
  NoDesk           RSS feed ("Role at Company")
  Jobspresso       WordPress job-board RSS feed
  Remote Rocketship  paid API (needs REMOTEROCKETSHIP_API_KEY in .env); skipped without a key

Every job is marked remote, and its location says who it's open to, e.g.
"Remote (Worldwide)" or "Remote (USA Only)", so the location rule in config.yaml
keeps only the ones you can take from Dubai.
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from ..models import RawJob
from .base import html_to_text, http_client, save_raw, to_iso

log = logging.getLogger("jobagent.collect.remote")


def _section(cfg, name: str) -> dict:
    return cfg.section("sources", name, default={}) or {}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _rss_items(xml_bytes: bytes) -> list[dict]:
    """Every <item> as {local-tag-name: text}; namespaced tags (job_listing:company) included."""
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        d: dict[str, str] = {}
        for child in item:
            name = _local(child.tag)
            if name not in d:                         # first one wins (e.g. several <category>)
                d[name] = (child.text or "").strip()
        out.append(d)
    return out


# ---------------------------------------------------------------- Himalayas

def collect_himalayas(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    c = _section(cfg, "himalayas")
    queries = c.get("queries") or ["software engineer"]
    country = c.get("country", "AE")
    pages = int(c.get("max_pages", 3))
    out, seen = [], set()
    with http_client() as client:
        for q in queries:
            for page in range(1, pages + 1):
                r = client.get("https://himalayas.app/jobs/api/search",
                               params={"q": q, "country": country, "sort": "recent", "page": page})
                if r.status_code == 429:
                    log.warning("himalayas: rate limited; stopping for today")
                    return out
                r.raise_for_status()
                jobs = r.json().get("jobs", [])
                save_raw(raw_dir, f"himalayas_{q}_{page}", jobs)
                for j in jobs:
                    gid = str(j.get("guid") or j.get("applicationLink") or "")
                    if not gid or gid in seen:
                        continue
                    seen.add(gid)
                    restr = j.get("locationRestrictions") or []
                    names = [x.get("name", "") if isinstance(x, dict) else str(x) for x in restr]
                    where = ", ".join(n for n in names if n) or "Worldwide"
                    salary = ""
                    if j.get("minSalary"):
                        salary = f"{j.get('currency', 'USD')} {j['minSalary']:,}-{j.get('maxSalary') or j['minSalary']:,} {j.get('salaryPeriod', 'annual')}"
                    url = gid if gid.startswith("http") else (j.get("applicationLink") or "")
                    out.append(RawJob(
                        source="himalayas", external_id=gid, title=j.get("title", ""),
                        company=j.get("companyName", ""), location=f"Remote ({where})", remote=True,
                        url=url, apply_url=j.get("applicationLink") or url,
                        description=html_to_text(j.get("description")) or j.get("excerpt", ""),
                        salary=salary, posted_at=to_iso(j.get("pubDate"))))
                if len(jobs) < 10:
                    break
                time.sleep(1)                         # they rate-limit; be gentle
    log.info("himalayas: %d jobs open to %s", len(out), country)
    return out


# ---------------------------------------------------------------- Remotive

def collect_remotive(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    c = _section(cfg, "remotive")
    with http_client() as client:
        r = client.get("https://remotive.com/api/remote-jobs",
                       params={"category": c.get("category", "software-dev"), "limit": c.get("limit", 300)})
        r.raise_for_status()
        data = r.json().get("jobs", [])
    save_raw(raw_dir, "remotive", data)
    out = []
    for j in data:
        where = (j.get("candidate_required_location") or "Worldwide").strip()
        out.append(RawJob(
            source="remotive", external_id=str(j.get("id", "")), title=j.get("title", ""),
            company=j.get("company_name", ""), location=f"Remote ({where})", remote=True,
            url=j.get("url", ""), apply_url=j.get("url", ""),
            description=html_to_text(j.get("description")), salary=j.get("salary") or "",
            posted_at=to_iso(j.get("publication_date"))))
    log.info("remotive: %d jobs", len(out))
    return out


# ---------------------------------------------------------------- Working Nomads

def collect_workingnomads(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    c = _section(cfg, "workingnomads")
    cats = {x.lower() for x in (c.get("categories") or ["Development"])}
    with http_client() as client:
        r = client.get("https://www.workingnomads.com/api/exposed_jobs/")
        r.raise_for_status()
        data = r.json()
    save_raw(raw_dir, "workingnomads", data)
    out = []
    for j in data if isinstance(data, list) else []:
        if cats and (j.get("category_name") or "").lower() not in cats:
            continue
        where = (j.get("location") or "Worldwide").strip()
        out.append(RawJob(
            source="workingnomads", external_id=j.get("url", ""), title=j.get("title", ""),
            company=j.get("company_name", ""), location=f"Remote ({where})", remote=True,
            url=j.get("url", ""), apply_url=j.get("url", ""),
            description=html_to_text(j.get("description")), posted_at=to_iso(j.get("pub_date"))))
    log.info("workingnomads: %d jobs in %s", len(out), ", ".join(sorted(cats)))
    return out


# ---------------------------------------------------------------- NoDesk

_PAREN = re.compile(r"\(([^)]*)\)")


def collect_nodesk(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    with http_client() as client:
        r = client.get("https://nodesk.co/remote-jobs/index.xml")
        r.raise_for_status()
    save_raw(raw_dir, "nodesk", r.text)
    out = []
    for it in _rss_items(r.content):
        raw_title = it.get("title", "")
        title, _, company = raw_title.rpartition(" at ")
        if not title:
            title, company = raw_title, ""
        # Restrictions live in the title, e.g. "Staff Analyst (USA Only, 100% Remote) at Close"
        notes = _PAREN.findall(title)
        clean_title = _PAREN.sub("", title).strip(" -,")
        where = "; ".join(notes) if notes else "Worldwide"
        link = it.get("link", "")
        out.append(RawJob(
            source="nodesk", external_id=link, title=clean_title, company=company.strip(),
            location=f"Remote ({where})", remote=True, url=link, apply_url=link,
            description=html_to_text(it.get("description")), posted_at=to_iso(it.get("pubdate"))))
    log.info("nodesk: %d jobs", len(out))
    return out


# ---------------------------------------------------------------- Jobspresso

def collect_jobspresso(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    """Standard WP Job Manager feed: <job_listing:company>, <job_listing:location>."""
    c = _section(cfg, "jobspresso")
    params = {"feed": "job_feed"}
    if c.get("category"):
        params["job_categories"] = c["category"]
    with http_client() as client:
        r = client.get("https://jobspresso.co/", params=params)
        r.raise_for_status()
    save_raw(raw_dir, "jobspresso", r.text)
    out = []
    for it in _rss_items(r.content):
        link = it.get("link", "")
        where = it.get("location") or "Worldwide"
        out.append(RawJob(
            source="jobspresso", external_id=link, title=it.get("title", ""),
            company=it.get("company", "") or it.get("creator", ""), location=f"Remote ({where})",
            remote=True, url=link, apply_url=link,
            description=html_to_text(it.get("encoded") or it.get("description")),
            posted_at=to_iso(it.get("pubdate"))))
    log.info("jobspresso: %d jobs", len(out))
    return out


# ---------------------------------------------------------------- Remote Rocketship (paid)

def collect_remoterocketship(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    key = os.environ.get("REMOTEROCKETSHIP_API_KEY")
    if not key:
        log.info("remoterocketship: no REMOTEROCKETSHIP_API_KEY in .env; skipped (paid API)")
        return []
    c = _section(cfg, "remoterocketship")
    where = c.get("locations") or ["United Arab Emirates"]
    out, seen = [], set()
    with httpx.Client(timeout=60) as client:
        for page in range(1, int(c.get("max_pages", 2)) + 1):
            body = {"filters": {
                "jobTitleFilters": c.get("titles") or ["Software Engineer", "Backend Engineer"],
                "locationFilters": where,
                "seniorityFilters": c.get("seniority") or ["senior", "expert"],
                "itemsPerPage": 50, "page": page, "sortBy": "DateAdded"},
                "includeJobDescription": True}
            r = client.post("https://www.remoterocketship.com/api/openclaw/jobs/", json=body,
                            headers={"Authorization": f"Bearer {key}"})
            if r.status_code in (401, 403, 429):
                log.warning("remoterocketship: HTTP %s (%s); stopping", r.status_code, r.text[:200])
                break
            r.raise_for_status()
            data = r.json()
            save_raw(raw_dir, f"remoterocketship_{page}", data)
            for j in data.get("jobOpenings", []):
                if data.get("sandbox"):
                    continue                          # fictional sample; never a real job
                jid = str(j.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                desc = "\n\n".join(x for x in (j.get("roleDescription"), j.get("roleRequirements")) if x)
                loc = j.get("location") or j.get("locations") or ", ".join(where)
                if isinstance(loc, list):
                    loc = ", ".join(str(x) for x in loc)
                out.append(RawJob(
                    source="remoterocketship", external_id=jid, title=j.get("roleTitle", ""),
                    company=((j.get("company") or {}).get("name") or ""),
                    location=f"Remote ({loc})", remote=True, url=j.get("url", ""), apply_url=j.get("url", ""),
                    description=html_to_text(desc), salary=str(j.get("salaryRange") or "")[:120]))
            if not (data.get("pagination") or {}).get("hasNextPage"):
                break
    log.info("remoterocketship: %d jobs", len(out))
    return out
