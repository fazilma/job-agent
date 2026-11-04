"""Add one job by its link (the dashboard's "Add job" box).

For jobs the daily searches miss: paste a LinkedIn (or any) job URL, and the posting is
fetched, merged with any copy already collected (same matching as the daily run), and scored.

- LinkedIn: its public job page (no login), the same one a logged-out browser sees.
- Anything else: the page's schema.org JobPosting block, which most career sites and
  job boards include for Google; otherwise the page title and main text.
If a page can't be read (blocked, login wall), the dashboard asks you to paste the details.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import db, rules
from .collectors.agencies import find_jobposting, ld_location, page_text
from .collectors.base import html_to_text, http_client
from .models import ACTIVE, FILTERED, RawJob
from .normalize import fingerprint

log = logging.getLogger("jobagent.addlink")

LINKEDIN_ID = re.compile(r"linkedin\.com.*?(?:currentJobId=|/jobs/view/(?:[^/?#]*?-)?)(\d{6,})", re.I)
SOURCE_BY_HOST = [("linkedin.com", "linkedin"), ("greenhouse.io", "greenhouse"), ("lever.co", "lever"),
                  ("ashbyhq.com", "ashby"), ("smartrecruiters.com", "smartrecruiters"), ("dover.com", "dover"),
                  ("bayt.com", "bayt"), ("naukrigulf.com", "naukrigulf")]


class FetchError(RuntimeError):
    """The page couldn't be read; ask for the details by hand."""


def source_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for part, src in SOURCE_BY_HOST:
        if host == part or host.endswith("." + part):
            return src
    return "manual"


def linkedin_id(url: str) -> str | None:
    m = LINKEDIN_ID.search(url or "")
    return m.group(1) if m else None


def _txt(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def parse_linkedin(html: str) -> dict:
    """Fields from LinkedIn's public job page (guest view)."""
    soup = BeautifulSoup(html, "html.parser")
    ld = find_jobposting(html)
    title = _txt(soup.select_one("h2.top-card-layout__title, h1.top-card-layout__title, h1"))
    company = _txt(soup.select_one("a.topcard__org-name-link, span.topcard__flavor a, span.topcard__flavor"))
    location = _txt(soup.select_one("span.topcard__flavor--bullet"))
    desc_node = soup.select_one("div.show-more-less-html__markup, div.description__text")
    description = desc_node.get_text("\n", strip=True) if desc_node else ""
    if ld:                                            # fill gaps from the JobPosting block
        title = title or ld.get("title") or ""
        org = ld.get("hiringOrganization") or {}
        company = company or (org.get("name") if isinstance(org, dict) else "") or ""
        location = location or ld_location(ld)
        description = description or html_to_text(ld.get("description"))
    criteria = {_txt(li.select_one("h3")).lower(): _txt(li.select_one("span"))
                for li in soup.select("li.description__job-criteria-item")}
    return {"title": title, "company": company, "location": location, "description": description,
            "posted": _txt(soup.select_one("span.posted-time-ago__text")),
            "employment": criteria.get("employment type", "")}


def parse_generic(html: str) -> dict:
    ld = find_jobposting(html)
    if ld:
        org = ld.get("hiringOrganization") or {}
        return {"title": ld.get("title") or "",
                "company": (org.get("name") if isinstance(org, dict) else str(org)) or "",
                "location": ld_location(ld), "description": html_to_text(ld.get("description")),
                "remote": (ld.get("jobLocationType") or "").upper() == "TELECOMMUTE",
                "posted": ld.get("datePosted") or "", "expires": ld.get("validThrough") or ""}
    soup = BeautifulSoup(html, "html.parser")
    og = lambda p: (soup.find("meta", property=p) or {}).get("content", "")
    title = og("og:title") or _txt(soup.find("title"))
    return {"title": title, "company": og("og:site_name"), "location": "", "description": page_text(html)}


def fetch(url: str, client=None) -> RawJob:
    """Read one posting. Raises FetchError when nothing usable comes back."""
    url = url.strip()
    if not re.match(r"https?://", url):
        raise FetchError("That doesn't look like a link (it should start with https://).")
    source = source_for(url)
    own = client is None
    client = client or http_client()
    try:
        li = linkedin_id(url) if source == "linkedin" else None
        if source == "linkedin" and not li:
            raise FetchError("Couldn't find a job ID in that LinkedIn link. Open the job and copy its link "
                             "(it looks like linkedin.com/jobs/view/1234567890).")
        pages = ([f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{li}",
                  f"https://www.linkedin.com/jobs/view/{li}/"] if li else [url])
        fields, last_err = None, ""
        for page in pages:
            try:
                resp = client.get(page)
                resp.raise_for_status()
            except Exception as e:
                last_err = str(e)[:200]
                continue
            f = parse_linkedin(resp.text) if li else parse_generic(resp.text)
            if f.get("title") and f.get("company"):
                fields = f
                break
            last_err = "the page had no job title/company (login wall or a changed layout)"
        if not fields:
            raise FetchError(f"Couldn't read the job from that page ({last_err}). Paste the details below instead.")
    finally:
        if own:
            client.close()
    canonical = f"https://www.linkedin.com/jobs/view/{li}/" if li else url
    loc = fields.get("location") or ""
    return RawJob(source=source, title=fields["title"].strip(), company=fields["company"].strip(),
                  location=loc, url=canonical, apply_url=canonical, description=fields.get("description") or "",
                  external_id=li or "", remote=bool(fields.get("remote")) or "remote" in loc.lower())


def add(conn, cfg, raw: RawJob) -> dict:
    """Save the job (merging with an existing copy) and return what happened. You chose this
    job yourself, so the filter rules don't drop it; if an existing copy was filtered out, it
    comes back to Active."""
    fp = fingerprint(raw.company, raw.title, raw.location, raw.remote)
    before = conn.execute("SELECT id, status, sources FROM jobs WHERE fingerprint=? OR (url=? AND url != '')",
                          (fp, raw.url)).fetchone()
    keep, reason = rules.check(raw, cfg.section("filter", default={}) or {})
    job_id, is_new = db.upsert_job(conn, raw, fp, run_id=None)
    row = db.get_job(conn, job_id)
    if row["status"] == FILTERED:
        conn.execute("UPDATE jobs SET filter_reason=NULL WHERE id=?", (job_id,))
        db.set_status(conn, job_id, ACTIVE, "added by link")
    if rules.gig_reason(raw.title, raw.description) and not row["gig"]:
        conn.execute("UPDATE jobs SET gig=1 WHERE id=?", (job_id,))
    conn.commit()
    row = db.get_job(conn, job_id)
    return {"id": job_id, "is_new": is_new, "merged_with": None if is_new else before and before["sources"],
            "title": row["title"], "company": row["company"], "location": row["location"],
            "source": row["primary_source"], "status": row["status"], "scored": row["score"] is not None,
            "rule_note": None if keep else reason}
