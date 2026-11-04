"""UAE recruitment agencies, scraped with plain HTTP + BeautifulSoup (no browser, no Apify).

Adapted from wazifa (https://github.com/ameer-khan-ashraf/wazifa, MIT License,
Copyright (c) 2026 Ameer Khan). Selectors and endpoints are theirs; the structure
(sync httpx, detail fetch for new relevant jobs, RawJob output) is ours.

Methods, most to least robust:
  sitemap + JSON-LD : Hays, Umatr
  JSON API          : Marc Ellis (WordPress REST), Discovered.ae
  HTML listing      : Halian, Charterhouse, Michael Page, Salt, Jex Digital

Agencies rarely name the client, so `company` is the agency, e.g. "Hays (recruiter)".
Listing pages usually lack the full description; for jobs that are new (URL not in
the DB yet) and whose title passes the filter, we fetch the job page once and read
its schema.org JobPosting block, falling back to the page's main text.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from .. import rules
from ..models import RawJob
from .base import html_to_text, http_client, save_raw, to_iso

log = logging.getLogger("jobagent.collect.agencies")


# ---------------------------------------------------------------- shared helpers

class Ctx:
    """Per-run state shared by the site scrapers."""

    def __init__(self, cfg, client: httpx.Client, raw_dir: Path | None):
        self.cfg = cfg
        self.client = client
        self.raw_dir = raw_dir
        ac = cfg.section("sources", "agencies", default={}) or {}
        self.max_pages = int(ac.get("max_pages", 10))
        self.max_details = int(ac.get("max_detail_fetches_per_site", 40))
        self.delay = tuple(ac.get("delay_seconds", [0.8, 2.5]))
        self.keywords = ac.get("salt_keywords") or ["software engineer", "backend", "developer"]
        self.filter = cfg.section("filter", default={}) or {}
        self.known = _known_urls(cfg)

    def get(self, url: str, **kw) -> httpx.Response:
        time.sleep(random.uniform(*self.delay))          # be polite: one request every ~1-2s
        r = self.client.get(url, **kw)
        r.raise_for_status()
        return r

    def title_ok(self, title: str) -> bool:
        """Cheap pre-check so we only fetch detail pages for roles you'd keep."""
        t = title or ""
        return bool(rules._has_any(t, self.filter.get("title_include"))) and \
            not rules._has_any(t, self.filter.get("title_exclude"))

    def is_new(self, url: str) -> bool:
        return url not in self.known


def _known_urls(cfg) -> set[str]:
    try:
        p = cfg.path("db")
        if not p.exists():
            return set()
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return {r[0] for r in conn.execute("SELECT url FROM jobs WHERE url IS NOT NULL")}
        finally:
            conn.close()
    except Exception:
        return set()


def find_jobposting(html: str) -> dict | None:
    """The schema.org JobPosting block Google requires for job search."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if isinstance(d, dict):
                if d.get("@type") == "JobPosting" or (isinstance(d.get("@type"), list) and "JobPosting" in d["@type"]):
                    return d
                if isinstance(d.get("@graph"), list):
                    stack.extend(d["@graph"])
    return None


def ld_location(ld: dict) -> str:
    locs = ld.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    out: list[str] = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            parts = [addr]
        else:
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
        for p in parts:
            if p and isinstance(p, str) and p not in out:
                out.append(p)
    if (ld.get("jobLocationType") or "").upper() == "TELECOMMUTE" and "Remote" not in out:
        out.append("Remote")
    return ", ".join(out)


def page_text(html: str, limit: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    node = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n\s*\n+", "\n\n", node.get_text("\n", strip=True))
    return text[:limit]


def fill_from_detail(ctx: Ctx, job: RawJob, budget: list[int]) -> RawJob:
    """Fetch the job page once (new + relevant jobs only) to get description/location."""
    if budget[0] <= 0 or not job.url or not ctx.is_new(job.url) or not ctx.title_ok(job.title):
        return job
    if len(job.description or "") >= 600 and job.location:
        return job
    budget[0] -= 1
    try:
        html = ctx.get(job.url).text
    except Exception as e:
        log.debug("detail fetch failed %s: %s", job.url, e)
        return job
    ld = find_jobposting(html)
    if ld:
        job.title = (ld.get("title") or job.title).strip()
        desc = html_to_text(ld.get("description"))
        if len(desc) > len(job.description or ""):
            job.description = desc
        job.location = job.location or ld_location(ld)
        job.posted_at = job.posted_at or to_iso(ld.get("datePosted"))
        job.remote = job.remote or (ld.get("jobLocationType") or "").upper() == "TELECOMMUTE"
        org = (ld.get("hiringOrganization") or {})
        org_name = org.get("name") if isinstance(org, dict) else None
        agency = job.extra.get("agency", "")
        if org_name and agency and agency.lower() not in org_name.lower():
            job.company = org_name.strip()                # the real client, when the agency names it
    else:
        text = page_text(html)
        if len(text) > len(job.description or ""):
            job.description = text
    return job


def sitemap_urls(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def _job(agency: str, source: str, title: str, url: str, location: str = "",
         description: str = "", posted_at: str = "", external_id: str = "") -> RawJob:
    return RawJob(source=source, title=(title or "").strip(), company=f"{agency} (recruiter)",
                  location=location or "", url=url, apply_url=url, description=description or "",
                  posted_at=posted_at or "", external_id=external_id,
                  remote=bool(re.search(r"\bremote\b", location or "", re.I)),
                  extra={"agency": agency})


def _slug_title(slug: str) -> str:
    return unquote(slug).replace("-", " ").replace("_", " ").strip().title()


# ---------------------------------------------------------------- sitemap + JSON-LD

def hays(ctx: Ctx) -> list[RawJob]:
    base = "https://www.hays.ae"
    urls = [u for u in sitemap_urls(ctx.get(f"{base}/sitemap/en-AE/job-sitemap.xml").text) if "/job-detail/" in u]
    jobs, seen, budget = [], set(), [ctx.max_details]
    for url in urls:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        ext_id = slug.rsplit("_", 1)[-1]
        if ext_id in seen:
            continue
        seen.add(ext_id)
        title = _slug_title(slug.rsplit("_", 1)[0])
        # Only relevant titles are kept at all: the sitemap lists every Hays job in the UAE.
        if not ctx.title_ok(title):
            continue
        job = fill_from_detail(ctx, _job("Hays", "hays", title, url, external_id=ext_id), budget)
        jobs.append(job)
    return jobs


def umatr(ctx: Ctx) -> list[RawJob]:
    base = "https://www.umatr.io"
    slug_re = re.compile(r"/jobs/(\d+)-([^/]+)/?$")
    urls = [u for u in sitemap_urls(ctx.get(f"{base}/sitemap.xml").text) if slug_re.search(u)]
    if not urls:
        soup = BeautifulSoup(ctx.get(f"{base}/jobs/").text, "html.parser")
        urls = [(a["href"] if a["href"].startswith("http") else base + a["href"])
                for a in soup.select("a[href*='/jobs/']") if slug_re.search(a["href"])]
    jobs, seen, budget = [], set(), [ctx.max_details]
    for url in urls:
        m = slug_re.search(url)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        title = _slug_title(m.group(2))
        if not ctx.title_ok(title):
            continue
        jobs.append(fill_from_detail(ctx, _job("Umatr", "umatr", title, url, external_id=m.group(1)), budget))
    return jobs


# ---------------------------------------------------------------- JSON APIs

def marcellis(ctx: Ctx) -> list[RawJob]:
    base = "https://www.marc-ellis.com"
    jobs, budget = [], [ctx.max_details]
    for page in range(1, ctx.max_pages + 1):
        r = ctx.get(f"{base}/wp-json/wp/v2/job-listings", params={"per_page": 100, "page": page})
        data = r.json()
        if not data:
            break
        save_raw(ctx.raw_dir, f"marcellis_p{page}", data)
        for item in data:
            title = html_to_text((item.get("title") or {}).get("rendered", "") if isinstance(item.get("title"), dict) else item.get("title", ""))
            url = item.get("link", "")
            if not title or not url:
                continue
            excerpt = (item.get("excerpt") or {}).get("rendered", "") if isinstance(item.get("excerpt"), dict) else item.get("excerpt", "")
            content = (item.get("content") or {}).get("rendered", "") if isinstance(item.get("content"), dict) else ""
            job = _job("Marc Ellis", "marcellis", title, url, description=html_to_text(content or excerpt),
                       posted_at=to_iso(item.get("date")), external_id=str(item.get("id", "")))
            jobs.append(fill_from_detail(ctx, job, budget))   # listing has no location
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            break
    return jobs


def discovered(ctx: Ctx) -> list[RawJob]:
    base = "https://www.discovered.ae"
    jobs, seen = [], set()
    for page in range(1, ctx.max_pages + 1):
        data = ctx.get(f"{base}/api/v1/jobs", params={"per_page": 100, "page": page}).json()
        save_raw(ctx.raw_dir, f"discovered_p{page}", data)
        for item in data.get("jobs", []):
            jid = str(item.get("id", ""))
            title = (item.get("job_title") or item.get("title") or "").strip()
            if not jid or not title or jid in seen:
                continue
            seen.add(jid)
            slug = item.get("cached_slug", "")
            jobs.append(_job("Discovered", "discovered", title, f"{base}/job/{slug}" if slug else base,
                             location=item.get("job_location") or "",
                             description=item.get("clean_description") or html_to_text(item.get("description")),
                             posted_at=to_iso(item.get("published_at") or item.get("created_at")),
                             external_id=jid))
        if page >= int(data.get("page_count", 1) or 1):
            break
    return jobs


# ---------------------------------------------------------------- HTML listings

def halian(ctx: Ctx) -> list[RawJob]:
    base = "https://www.halian.com"
    soup = BeautifulSoup(ctx.get(f"{base}/jobs").text, "html.parser")
    id_re, city_re, ctry_re = re.compile(r"-(\d+)$"), re.compile(r"js-city--([\w-]+)"), re.compile(r"js-country--([\w-]+)")
    jobs, seen, budget = [], set(), [ctx.max_details]
    for item in soup.select("li.js-job-item"):
        a = item.select_one("a[href*='/jobs/']")
        if not a:
            continue
        href = a["href"].rstrip("/")
        m = id_re.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        p = a.find("p")
        title = p.get_text(strip=True) if p else ""
        if not title:
            continue
        classes = " ".join(item.get("class", []))
        loc = ", ".join(_slug_title(mm.group(1)) for mm in (city_re.search(classes), ctry_re.search(classes)) if mm)
        url = href if href.startswith("http") else base + href
        jobs.append(fill_from_detail(ctx, _job("Halian", "halian", title, url, location=loc, external_id=m.group(1)), budget))
    return jobs


def charterhouse(ctx: Ctx) -> list[RawJob]:
    base = "https://www.charterhouseme.ae"
    jobs, seen, budget = [], set(), [ctx.max_details]
    for page in range(1, ctx.max_pages + 1):
        soup = BeautifulSoup(ctx.get(f"{base}/jobs/information-technology", params={"page": page}).text, "html.parser")
        items = soup.select("li.job-result-item")
        new = 0
        for item in items:
            a = item.select_one(".job-title a[href^='/job/']")
            if not a:
                continue
            href = a["href"].rstrip("/")
            slug, title = href.split("/job/")[-1], a.get_text(strip=True)
            if not title or slug in seen:
                continue
            seen.add(slug)
            new += 1
            loc = item.select_one(".results-job-location")
            desc = item.select_one(".job-description")
            jobs.append(fill_from_detail(ctx, _job(
                "Charterhouse", "charterhouse", title, base + href,
                location=loc.get_text(strip=True) if loc else "",
                description=desc.get_text(" ", strip=True) if desc else "", external_id=slug), budget))
        if not items or new == 0:
            break
    return jobs


def michaelpage(ctx: Ctx) -> list[RawJob]:
    base = "https://www.michaelpage.ae"
    ref_re = re.compile(r"(jn-\d+-\d+)", re.I)
    jobs, seen, budget = [], set(), [ctx.max_details]
    for page in range(0, ctx.max_pages):                    # 0-indexed on Michael Page
        try:
            html_text = ctx.get(f"{base}/jobs/technology", params={"page": page}).text
        except httpx.HTTPStatusError as e:
            if page and e.response.status_code == 404:      # past the last page: keep what we have
                break
            raise
        soup = BeautifulSoup(html_text, "html.parser")
        tiles = soup.select("h3 a[href*='/job-detail/'], a[href*='/job-detail/']")
        new = 0
        for a in tiles:
            href, title = a.get("href", ""), a.get_text(strip=True)
            if not href or not title:
                continue
            m = ref_re.search(href)
            ext = m.group(1) if m else href.rstrip("/").split("/")[-1]
            if ext in seen:
                continue
            seen.add(ext)
            new += 1
            loc, card = "", a
            for _ in range(6):                               # location sits a few levels up
                card = card.parent
                if card is None:
                    break
                tag = card.select_one(".job-location")
                if tag:
                    loc = tag.get_text(strip=True)
                    break
            url = href if href.startswith("http") else base + href
            jobs.append(fill_from_detail(ctx, _job("Michael Page", "michaelpage", title, url, location=loc, external_id=ext), budget))
        if not tiles or new == 0:
            break
    return jobs


def salt(ctx: Ctx) -> list[RawJob]:
    base = "https://welovesalt.com"
    id_re = re.compile(r"-(\d+)$")
    jobs, seen, budget = [], set(), [ctx.max_details]
    for kw in ctx.keywords:
        for page in range(1, ctx.max_pages + 1):
            soup = BeautifulSoup(ctx.get(f"{base}/jobs", params={"k": kw, "country": 342, "page": page}).text, "html.parser")
            links = soup.select(".job-item__title a[href]") or [
                a for a in soup.find_all("a", href=True)
                if re.search(r"-\d{4,}$", a["href"]) and f"{base}/jobs/" in a["href"]]
            new = 0
            for a in links:
                href, title = a["href"], a.get_text(strip=True)
                if not title:
                    continue
                m = id_re.search(href)
                ext = m.group(1) if m else href.rstrip("/").split("/")[-1]
                if ext in seen:
                    continue
                seen.add(ext)
                new += 1
                loc, li = "", a.find_parent("li", class_="job-item")
                if li:
                    icon = li.select_one(".highlights__item-icon--location")
                    span = icon.find_next_sibling("span") if icon else None
                    loc = span.get_text(strip=True) if span else ""
                url = href if href.startswith("http") else base + href
                jobs.append(fill_from_detail(ctx, _job("Salt", "salt", title, url, location=loc, external_id=ext), budget))
            if not links or new == 0:
                break
    return jobs


def jexdigital(ctx: Ctx) -> list[RawJob]:
    base = "https://www.jexdigital.co"
    slug_re = re.compile(r"jexdigital\.co/jobs/([^/?#]+)")
    loc_re = re.compile(r"Location\s+(.+?)\s+(?:Level|Salary|Know More|$)")
    soup = BeautifulSoup(ctx.get(f"{base}/jobs").text, "html.parser")
    jobs, seen, budget = [], set(), [ctx.max_details]
    for a in soup.find_all("a", href=True):
        m = slug_re.search(a["href"]) or re.match(r"^/jobs/([^/?#]+)/?$", a["href"])
        if not m or m.group(1) in seen:
            continue
        slug = m.group(1)
        seen.add(slug)
        card = a.find_parent("div") or a.find_parent("li") or a.find_parent("article")
        loc, node = "", card
        for _ in range(8):                                   # climb until one job's fields are inside
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if text.count("Location") == 1:
                card = node
                mm = loc_re.search(text)
                loc = mm.group(1).strip() if mm else ""
                break
            node = node.parent
        heading = card.find(re.compile(r"^h[1-4]$")) if card else None
        title = heading.get_text(strip=True) if heading else _slug_title(slug)
        url = a["href"] if a["href"].startswith("http") else base + a["href"]
        jobs.append(fill_from_detail(ctx, _job("Jex Digital", "jexdigital", title, url, location=loc, external_id=slug), budget))
    return jobs


# Sites that only list UAE roles (or are searched with a UAE filter): a blank
# location there means UAE. Halian and Marc Ellis also list other countries.
UAE_ONLY = {"hays", "umatr", "discovered", "charterhouse", "michaelpage", "salt", "jexdigital"}

SITES: dict[str, Callable[[Ctx], list[RawJob]]] = {
    "hays": hays, "umatr": umatr, "marcellis": marcellis, "discovered": discovered,
    "halian": halian, "charterhouse": charterhouse, "michaelpage": michaelpage,
    "salt": salt, "jexdigital": jexdigital,
}


def collect(cfg, raw_dir: Path | None = None, sites: list[str] | None = None) -> list[RawJob]:
    from .. import parallel

    ac = cfg.section("sources", "agencies", default={}) or {}
    wanted = [n for n in (sites or ac.get("sites") or list(SITES)) if n in SITES]
    for n in set(sites or ac.get("sites") or []) - set(SITES):
        log.warning("agencies: unknown site %r", n)
    out: list[RawJob] = []
    with http_client() as client:
        ctx = Ctx(cfg, client, raw_dir)
        # Different sites run side by side; requests to the same site stay one at a time
        # with a polite pause in between.
        results = parallel.run_all({n: (lambda n=n: SITES[n](ctx)) for n in wanted},
                                   max_workers=len(wanted) or 1,
                                   deadline_s=float(ac.get("timeout_minutes", 10)) * 60)
        for name in wanted:
            jobs, secs = results[name]
            if isinstance(jobs, BaseException):          # one broken site never stops the others
                log.warning("agency %s failed after %.0fs: %s", name, secs, jobs)
                continue
            if name in UAE_ONLY:
                for j in jobs:
                    j.location = j.location or "UAE"
            log.info("agency %s: %d jobs (%.0fs)", name, len(jobs), secs)
            out.extend(jobs)
    return out
