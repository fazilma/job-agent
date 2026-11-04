"""Open-web job search through the Agent Reach CLI: no login, no Apify. Exa, the search service
behind it, has a free rate limit; a search stopped by it ends that run's searching early.

Finds postings on sites no other collector reads (company sites, Teamtailor, smaller boards).
Search results are mostly noise (listing pages, dead links, old posts), so a result counts only
if its page has a schema.org JobPosting block with a recent date.

These pages come from anywhere, so their text is untrusted: db.jobs_to_tailor never tailors
them automatically, and clicking Tailor runs them without web access (tailor.tailor_one).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .. import rules
from ..models import RawJob
from .base import http_client, save_raw, to_iso

log = logging.getLogger("jobagent.collect.web_search")

SOURCE = "web_search"
# Listing pages and posts, not jobs; real LinkedIn postings come from the LinkedIn collectors.
SKIP_HOSTS = ("linkedin.com",)


def _bin() -> str | None:
    # launchd's PATH may not include ~/.local/bin, where Agent Reach installs itself.
    return shutil.which("agent-reach") or shutil.which("agent-reach", path=os.path.expanduser("~/.local/bin"))


def _search(query: str, limit: int) -> list[dict]:
    """One search. Raises RuntimeError on any failure, including rate limits."""
    try:
        out = subprocess.run([_bin(), "web", "search", query, "--limit", str(limit)],
                             capture_output=True, text=True, timeout=90)
        data = json.loads(out.stdout)
        ok, err, items = data.get("ok"), data.get("error") or {}, data.get("items") or []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as e:
        raise RuntimeError(f"agent-reach failed: {e}") from e
    if not ok:
        raise RuntimeError(f"agent-reach {err.get('code', 'error')}: {err.get('message', '')}"[:300])
    return items


def is_public_url(url: str) -> bool:
    """Search results are untrusted: never let one point us at this machine or the local network."""
    try:
        u = urlparse(url or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        addrs = {i[4][0] for i in socket.getaddrinfo(u.hostname, None)}
        return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)
    except (OSError, ValueError):       # shortcut: redirects aren't re-checked, re-check if fetching beyond GET
        return False


def collect(cfg, raw_dir: Path | None = None, client=None, search=_search) -> list[RawJob]:
    from ..addlink import parse_generic     # addlink imports collectors, so not at module level

    c = cfg.section("sources", "web_search", default={}) or {}
    queries = c.get("queries") or []
    if not queries:
        return []
    if search is _search and not _bin():
        log.info("web_search: agent-reach is not installed; skipped")
        return []

    hits: dict[str, dict] = {}
    error = None
    for q in queries:
        try:
            for item in search(q, int(c.get("results_per_query", 25))):
                hits.setdefault(item.get("url") or "", item)
        except RuntimeError as e:
            error = str(e)              # rate limited or blocked: don't push on with more searches
            log.warning("web_search: stopped at '%s': %s", q, e)
            break
    if error and not hits:
        raise RuntimeError(error)       # lets the circuit breaker count a source that's down
    save_raw(raw_dir, SOURCE, list(hits.values()))

    # Known jobs are read again on purpose: returning them is what keeps them from auto-closing.
    rule_cfg = cfg.section("filter", default={}) or {}
    today = date.today().isoformat()
    oldest = (date.today() - timedelta(days=int(c.get("max_age_days", 30)))).isoformat()
    own = client is None
    client = client or http_client()
    jobs: list[RawJob] = []
    seen: set[tuple[str, str]] = set()
    try:
        for url, item in hits.items():
            # Save page reads for results that could pass the filter. Only title rules: no company yet.
            if rules.title_check(RawJob(source=SOURCE, title=item.get("title") or "", company=""), rule_cfg):
                continue
            if not is_public_url(url):
                continue
            host = (urlparse(url).hostname or "").lower()
            if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
                continue
            try:
                r = client.get(url, timeout=10)
                r.raise_for_status()
                f = parse_generic(r.text)
                posted = to_iso(f.get("posted"))    # only pages with a JobPosting block have one
                expires = to_iso(f.get("expires"))
                if not posted or posted < oldest or posted > today or (expires and expires < today):
                    continue
                if not (f.get("title") and f.get("company") and f.get("description")):
                    continue
                title, company = f["title"].strip(), f["company"].strip()
                key = (company.casefold(), title.casefold())
                if key in seen:                     # the same job reposted on another site
                    continue
                seen.add(key)
                loc = f.get("location") or ""
                jobs.append(RawJob(source=SOURCE, title=title, company=company, location=loc, url=url, apply_url=url,
                                   description=f["description"], posted_at=posted,
                                   remote=bool(f.get("remote")) or "remote" in loc.lower()))
            except Exception as e:      # page data is untrusted: one bad page must not lose the run
                log.debug("web_search: %s failed: %s", url, e)
    finally:
        if own:
            client.close()
    log.info("web_search: %d postings from %d results", len(jobs), len(hits))
    return jobs
