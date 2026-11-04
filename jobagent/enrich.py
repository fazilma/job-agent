"""Fill in missing descriptions before scoring.

Some sources only give an excerpt (a few hundred characters). For those jobs we read
the posting page once and take its schema.org JobPosting description, or the page's
main text. LinkedIn pages are skipped (they block plain requests).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from . import db
from .collectors.agencies import find_jobposting, ld_location, page_text
from .collectors.base import html_to_text, http_client

log = logging.getLogger("jobagent.enrich")

SKIP_HOSTS = ("linkedin.com",)


def short_jobs(conn, min_chars: int, limit: int):
    return conn.execute(
        """SELECT * FROM jobs WHERE status IN ('active','opened') AND score IS NULL
             AND LENGTH(COALESCE(description,'')) < ? AND COALESCE(url,'') LIKE 'http%'
           ORDER BY first_seen DESC LIMIT ?""", (min_chars, limit)).fetchall()


def fill_short_descriptions(conn, cfg, client=None) -> int:
    ec = cfg.section("enrich", default={}) or {}
    if not ec.get("enabled", True):
        return 0
    min_chars = int(ec.get("min_chars", 800))
    rows = short_jobs(conn, min_chars, int(ec.get("max_per_run", 40)))
    if not rows:
        return 0
    own = client is None
    client = client or http_client()
    filled = 0
    try:
        for r in rows:
            url = r["url"]
            if any(h in (urlparse(url).hostname or "") for h in SKIP_HOSTS):
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as e:
                log.debug("enrich: %s failed: %s", url, e)
                continue
            ld = find_jobposting(resp.text)
            text = html_to_text(ld.get("description")) if ld else ""
            if len(text) < min_chars:
                text = max(text, page_text(resp.text), key=len)
            if len(text) > len(r["description"] or ""):
                updates = {"description": text}
                if ld and not r["location"]:
                    updates["location"] = ld_location(ld)
                sets = ", ".join(f"{k}=?" for k in updates)
                conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*updates.values(), r["id"]))
                filled += 1
            time.sleep(float(ec.get("delay_seconds", 0.5)))
        conn.commit()
    finally:
        if own:
            client.close()
    log.info("enrich: filled %d of %d short descriptions", filled, len(rows))
    return filled
