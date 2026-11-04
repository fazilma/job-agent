"""LinkedIn's public job search: free, no login, no Apify.

The same search a logged-out visitor sees at linkedin.com/jobs, read through the pages
LinkedIn itself loads as you scroll (10 jobs each). Two kinds of input in config.yaml:

- `urls`: LinkedIn search links copied from your browser. Their filters (keywords, place,
  date posted, remote, experience level...) are reused; the personal parts (currentJobId,
  "how you fit") are dropped, since a logged-out search can't see your profile.
- `searches`: keywords + location, like the Apify LinkedIn source.

LinkedIn answers "429 too many requests" if asked too fast, so requests are spaced out,
one pause-and-retry is allowed, and after that the source stops for the day with whatever it
has. Full descriptions are fetched only for jobs that are new to the tracker and pass your
filter rules, with a per-run cap.
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from .. import rules
from ..models import RawJob
from .base import http_client

log = logging.getLogger("jobagent.collect.linkedin_public")

SEARCH_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
# Filters a logged-out search understands; everything else in a pasted link is dropped.
KEEP_PARAMS = ("keywords", "location", "geoId", "f_TPR", "f_WT", "f_E", "f_JT", "f_C", "f_I", "f_F",
               "f_SB2", "f_AL", "distance", "sortBy")


class RateLimited(RuntimeError):
    pass


def params_from_url(url: str) -> dict:
    """The search filters in a LinkedIn jobs link (search or search-results pages)."""
    q = parse_qs(urlparse(url).query)
    return {k: q[k][0] for k in KEEP_PARAMS if q.get(k)}


def parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("[data-entity-urn]"):
        urn = card.get("data-entity-urn", "")
        m = re.search(r"jobPosting:(\d+)", urn)
        if not m:
            continue
        txt = lambda sel: re.sub(r"\s+", " ", (card.select_one(sel).get_text(" ", strip=True)
                                                if card.select_one(sel) else "")).strip()
        t = card.select_one("time")
        out.append({"id": m.group(1), "title": txt(".base-search-card__title"),
                    "company": txt(".base-search-card__subtitle"),
                    "location": txt(".job-search-card__location"),
                    "posted": t.get("datetime", "") if t else "",
                    "salary": txt(".job-search-card__salary-info")})
    return out


class _Client:
    """Spaced-out requests with one pause-and-retry on 429."""

    def __init__(self, delay: float, client=None, sleep=time.sleep):
        self.delay, self.sleep = delay, sleep
        self.http = client or http_client()
        self.first = True

    def get(self, url: str, params: dict | None = None) -> str:
        if not self.first:
            self.sleep(self.delay + random.uniform(0, self.delay / 2))
        self.first = False
        for attempt in range(2):
            r = self.http.get(url, params=params)
            if r.status_code == 429:
                if attempt == 0:
                    log.info("LinkedIn says too many requests; pausing 60s")
                    self.sleep(60)
                    continue
                raise RateLimited("LinkedIn rate limit (429)")
            r.raise_for_status()
            return r.text
        raise RateLimited("LinkedIn rate limit (429)")


def _known_ids(cfg) -> set[str]:
    from .apify_boards import known_linkedin_ids
    return set(known_linkedin_ids(cfg))


def collect(cfg, raw_dir: Path | None = None, client=None, sleep=time.sleep) -> list[RawJob]:
    c = cfg.section("sources", "linkedin_public", default={}) or {}
    past = c.get("date_posted", "r604800")
    queries: list[tuple[str, dict]] = []
    for url in c.get("urls") or []:
        p = params_from_url(url)
        if p:
            p.setdefault("f_TPR", past)
            queries.append((f"link: {p.get('keywords', '')} {p.get('geoId') or p.get('location', '')}".strip(), p))
    for s in c.get("searches") or []:
        p = {"keywords": s.get("keywords", ""), "f_TPR": s.get("date_posted", past)}
        if s.get("location"):
            p["location"] = s["location"]
        if s.get("geo_id"):
            p["geoId"] = str(s["geo_id"])
        queries.append((f"{p['keywords']} / {s.get('location', '')}", p))
    if not queries:
        return []

    max_per = int(c.get("max_per_search", 100))
    http = _Client(float(c.get("delay_seconds", 2.0)), client=client, sleep=sleep)
    cards: dict[str, dict] = {}
    try:
        for label, params in queries:
            got = 0
            for start in range(0, max_per, 10):
                page = parse_cards(http.get(SEARCH_API, {**params, "start": start}))
                new = [cd for cd in page if cd["id"] not in cards]
                for cd in new:
                    cards[cd["id"]] = cd
                got += len(page)
                if len(page) < 10:
                    break
            log.info("linkedin_public '%s': %d jobs", label, got)
    except RateLimited:
        log.warning("linkedin_public: stopped early by LinkedIn's rate limit; keeping %d jobs", len(cards))

    # Full descriptions: only for jobs new to the tracker that pass the filter, capped per run.
    from ..addlink import parse_linkedin
    known = _known_ids(cfg)
    rule_cfg = cfg.section("filter", default={}) or {}
    budget = int(c.get("details_per_run", 60))
    jobs: list[RawJob] = []
    stopped = False
    for cd in cards.values():
        job = RawJob(source="linkedin", title=cd["title"], company=cd["company"], location=cd["location"],
                     url=f"https://www.linkedin.com/jobs/view/{cd['id']}/", external_id=cd["id"],
                     posted_at=cd["posted"], salary=cd["salary"],
                     remote="remote" in f"{cd['title']} {cd['location']}".lower())
        job.apply_url = job.url
        # The location rule may need the description (e.g. "visa sponsorship"), so only the title is checked here.
        if not stopped and budget > 0 and cd["id"] not in known and rules.title_check(job, rule_cfg) is None:
            try:
                f = parse_linkedin(http.get(JOB_API.format(id=cd["id"])))
                job.description = f.get("description") or ""
                budget -= 1
            except RateLimited:
                stopped = True
                log.warning("linkedin_public: rate limit while reading descriptions; the rest get theirs next run")
            except Exception as e:
                log.debug("linkedin_public: description for %s failed: %s", cd["id"], e)
        jobs.append(job)
    log.info("linkedin_public: %d jobs from %d searches (%d new descriptions read)",
             len(jobs), len(queries), int(c.get("details_per_run", 60)) - budget)
    return jobs
