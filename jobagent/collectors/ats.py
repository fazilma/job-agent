"""Company career pages through public ATS job-board APIs (no scraping, no keys)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from ..models import RawJob
from .base import html_to_text, http_client, pick, save_raw, to_iso

log = logging.getLogger("jobagent.collect.ats")

ATS_KINDS = ("greenhouse", "lever", "ashby")


def _greenhouse(client: httpx.Client, token: str, company: str) -> list[RawJob]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = pick(j, "location.name")
        out.append(RawJob(
            source="greenhouse", external_id=str(j.get("id")), company=company,
            title=j.get("title", ""), location=loc, url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""), description=html_to_text(j.get("content")),
            remote="remote" in loc.lower(), posted_at=to_iso(pick(j, "first_published", "updated_at"))))
    return out


def _lever_salary(j: dict) -> str:
    sr = j.get("salaryRange") or {}
    if not sr.get("min"):
        return ""
    return f"{sr.get('min')}-{sr.get('max', '')} {sr.get('currency', '')} {sr.get('interval', '')}".strip()


def _lever(client: httpx.Client, token: str, company: str) -> list[RawJob]:
    r = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("unexpected lever payload")
    out = []
    for j in data:
        parts = [j.get("descriptionPlain", "")]
        for lst in j.get("lists", []) or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain", ""))
        loc = pick(j, "categories.location")
        all_locs = pick(j, "categories.allLocations", default=[])
        if isinstance(all_locs, list) and len(all_locs) > 1:
            loc = " / ".join(all_locs)
        out.append(RawJob(
            source="lever", external_id=j.get("id", ""), company=company, title=j.get("text", ""),
            location=loc, url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
            description="\n\n".join(p for p in parts if p).strip(),
            remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower(),
            salary=_lever_salary(j),
            posted_at=to_iso(j.get("createdAt"))))
    return out


def _ashby(client: httpx.Client, token: str, company: str) -> list[RawJob]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                   params={"includeCompensation": "true"})
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        loc = " / ".join(l for l in locs if l)
        out.append(RawJob(
            source="ashby", external_id=j.get("id", ""), company=company, title=j.get("title", ""),
            location=loc, url=j.get("jobUrl", ""), apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            # Ashby sets isRemote on some hybrid roles; trust workplaceType when it's there.
            remote=(j.get("workplaceType") == "Remote") if j.get("workplaceType") else bool(j.get("isRemote")),
            salary=pick(j, "compensation.compensationTierSummary"),
            posted_at=to_iso(j.get("publishedAt"))))
    return out


def _smartrecruiters(client: httpx.Client, token: str, company: str, *, countries=None,
                     title_ok=None, known: set | None = None, max_details: int = 60) -> list[RawJob]:
    """SmartRecruiters public API (e.g. Delivery Hero / talabat). Never auto-detected:
    it answers 200 with an empty list for any slug, so it must be set explicitly."""
    base = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    out, budget = [], max_details
    for country in countries or [None]:
        offset = 0
        while offset < 1000:
            params = {"limit": 100, "offset": offset}
            if country:
                params["country"] = country
            r = client.get(base, params=params)
            r.raise_for_status()
            data = r.json()
            content = data.get("content", [])
            for j in content:
                loc = j.get("location") or {}
                location = ", ".join(p for p in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if p)
                if loc.get("remote"):
                    location = f"{location}, Remote" if location else "Remote"
                url = f"https://jobs.smartrecruiters.com/{token}/{j['id']}"
                job = RawJob(source="smartrecruiters", external_id=str(j["id"]), company=company,
                             title=j.get("name", ""), location=location, url=url, apply_url=url,
                             remote=bool(loc.get("remote")), posted_at=to_iso(j.get("releasedDate")))
                # The list has no description; fetch it for new jobs with a relevant title.
                if budget > 0 and (title_ok is None or title_ok(job.title)) and url not in (known or set()):
                    budget -= 1
                    try:
                        d = client.get(j.get("ref") or f"{base}/{j['id']}").json()
                        sections = ((d.get("jobAd") or {}).get("sections") or {})
                        job.description = "\n\n".join(
                            f"{v.get('title', '')}\n{html_to_text(v.get('text'))}".strip()
                            for v in sections.values() if isinstance(v, dict) and v.get("text"))
                        job.apply_url = d.get("applyUrl") or url
                    except Exception as e:
                        log.debug("smartrecruiters detail %s: %s", j.get("id"), e)
                out.append(job)
            offset += len(content)
            if not content or offset >= int(data.get("totalFound", 0) or 0):
                break
    return out


DOVER = "https://app.dover.com/api/v1"


def _dover_location(locs: list, workplace: str) -> tuple[str, bool]:
    parts, remote = [], (workplace or "").upper() == "REMOTE"
    for l in locs or []:
        name = l.get("name") or (l.get("location_option") or {}).get("display_name") or ""
        if (l.get("location_type") or "").upper() == "REMOTE":
            remote = True
            parts.append(f"Remote ({name})" if name else "Remote")
        elif name:
            parts.append(name)
    return " / ".join(parts) or ("Remote" if remote else ""), remote


def _dover(client: httpx.Client, token: str, company: str, *, title_ok=None,
           known: set | None = None, max_details: int = 40) -> list[RawJob]:
    """Dover careers pages (app.dover.com/jobs/<slug>): public JSON behind the page."""
    page = client.get(f"{DOVER}/careers-page-slug/{token}")
    page.raise_for_status()
    info = page.json()
    cid, name = info["id"], info.get("name") or company
    out, budget, offset = [], max_details, 0
    while True:
        r = client.get(f"{DOVER}/careers-page/{cid}/jobs", params={"limit": 300, "offset": offset})
        r.raise_for_status()
        data = r.json()
        results = data.get("results", []) if isinstance(data, dict) else data
        for j in results:
            if not j.get("is_published", True) or j.get("is_sample"):
                continue
            loc, remote = _dover_location(j.get("locations"), j.get("workplace_type"))
            url = f"https://app.dover.com/apply/{name.replace(' ', '%20')}/{j['id']}/"
            job = RawJob(source="dover", external_id=j["id"], company=company or name,
                         title=j.get("title", ""), location=loc, remote=remote, url=url, apply_url=url)
            # The list has no description; read it for new jobs whose title you'd keep.
            if budget > 0 and (title_ok is None or title_ok(job.title)) and url not in (known or set()):
                budget -= 1
                try:
                    d = client.get(f"{DOVER}/inbound/application-portal-job/{j['id']}").json()
                    job.description = html_to_text(d.get("user_provided_description"))
                    if d.get("visa_support") is True:      # yes/no flag on the job
                        job.description += "\n\nVisa sponsorship available."
                    comp = d.get("compensation") or {}
                    if isinstance(comp, dict) and comp.get("lower_bound"):
                        hi = comp.get("upper_bound") or comp["lower_bound"]
                        job.salary = (f"{comp.get('currency_code') or ''} {comp['lower_bound']:,}-{hi:,} "
                                      f"{(comp.get('salary_range_type') or '').lower()}").strip()
                    elif isinstance(d.get("compensation_details"), str):
                        job.salary = d["compensation_details"][:120]
                    job.posted_at = to_iso(d.get("created"))
                except Exception as e:
                    log.debug("dover detail %s: %s", j.get("id"), e)
            out.append(job)
        offset += len(results)
        if not isinstance(data, dict) or not data.get("next") or not results:
            break
    return out


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def detect(client: httpx.Client, token: str, company: str, kinds=ATS_KINDS) -> tuple[str | None, list[RawJob]]:
    for kind in kinds:
        try:
            jobs = FETCHERS[kind](client, token, company)
            return kind, jobs
        except Exception:
            continue
    return None, []


def _discovered(cfg, configured: set[str]) -> list[dict]:
    """Boards found automatically (collectors/discover.py), minus ones already in config.yaml."""
    from .. import db
    from .discover import boards
    p = cfg.path("db")
    if not p.exists() or not (cfg.section("sources", "discover", default={}) or {}).get("enabled", True):
        return []
    try:
        conn = db.connect(p)
        try:
            return [b for b in boards(conn) if b["token"].lower() not in configured]
        finally:
            conn.close()
    except Exception as e:
        log.warning("couldn't read discovered career boards: %s", e)
        return []


def collect(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    from .. import parallel, rules
    from .agencies import _known_urls

    companies = list(cfg.section("sources", "companies", default=[]) or [])
    companies += _discovered(cfg, {str(c.get("token", "")).lower() for c in companies})
    if not companies:
        return []
    cache_path = cfg.path("db").parent / "ats_cache.json"
    cache = _load_cache(cache_path)
    flt = cfg.section("filter", default={}) or {}

    def title_ok(t):
        return bool(rules._has_any(t, flt.get("title_include"))) and not rules._has_any(t, flt.get("title_exclude"))

    known = _known_urls(cfg)
    out: list[RawJob] = []
    with http_client() as client:              # httpx.Client is safe to share across threads

        def fetch(c: dict) -> tuple[str | None, list[RawJob]]:
            name, token, ats = c["name"], c["token"], c.get("ats", "auto")
            if ats == "smartrecruiters":
                return "smartrecruiters", _smartrecruiters(client, token, name, countries=c.get("countries"),
                                                           title_ok=title_ok, known=known)
            if ats == "dover":
                return "dover", _dover(client, token, name, title_ok=title_ok, known=known)
            kinds = [cache[token]] if ats == "auto" and token in cache else (
                ATS_KINDS if ats == "auto" else [ats])
            kind, jobs = detect(client, token, name, kinds)
            if kind is None and ats == "auto" and token in cache:   # cached kind stopped working
                kind, jobs = detect(client, token, name)
            return kind, jobs

        tasks = {c["token"]: (lambda c=c: fetch(c)) for c in companies}
        workers = int(cfg.section("run", "parallel_requests", default=8))
        results = parallel.run_all(tasks, max_workers=workers, deadline_s=600)
        for c in companies:
            name, token = c["name"], c["token"]
            res, secs = results[token]
            if isinstance(res, BaseException):
                log.warning("career page %s failed after %.0fs: %s", name, secs, res)
                continue
            kind, jobs = res
            if kind is None:
                log.warning("career page: no ATS board found for %s (%s)", name, token)
                continue
            if kind not in ("smartrecruiters", "dover"):
                cache[token] = kind
            save_raw(raw_dir, f"ats_{token}", [j.__dict__ for j in jobs])
            log.info("career page %s via %s: %d jobs (%.0fs)", name, kind, len(jobs), secs)
            out.extend(jobs)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))
    return out


def check_companies(cfg) -> list[tuple[str, str, str | None, int]]:
    """For `jobagent check-companies`: which tokens resolve, and to which ATS."""
    results = []
    with http_client() as client:
        for c in cfg.section("sources", "companies", default=[]) or []:
            ats = c.get("ats", "auto")
            if ats == "dover":
                try:
                    n = len(_dover(client, c["token"], c["name"], max_details=0))
                    results.append((c["name"], c["token"], "dover", n))
                except Exception:
                    results.append((c["name"], c["token"], None, 0))
                continue
            if ats == "smartrecruiters":
                try:
                    n = len(_smartrecruiters(client, c["token"], c["name"], countries=c.get("countries"), max_details=0))
                    results.append((c["name"], c["token"], "smartrecruiters" if n else None, n))
                except Exception:
                    results.append((c["name"], c["token"], None, 0))
                continue
            kind, jobs = detect(client, c["token"], c["name"], ATS_KINDS if ats == "auto" else [ats])
            if kind is None and ats != "auto":
                kind, jobs = detect(client, c["token"], c["name"])
            results.append((c["name"], c["token"], kind, len(jobs)))
    return results
