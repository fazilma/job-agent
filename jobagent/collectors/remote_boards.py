"""Remote-first boards: Remote OK (JSON API) and We Work Remotely (RSS)."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models import RawJob
from .base import html_to_text, http_client, save_raw, to_iso

log = logging.getLogger("jobagent.collect.remote")


def collect_remoteok(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    want = {t.lower() for t in cfg.section("sources", "remoteok", "tags", default=[]) or []}
    with http_client() as client:
        r = client.get("https://remoteok.com/api")
        r.raise_for_status()
        data = r.json()
    save_raw(raw_dir, "remoteok", data)
    out = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:
            continue                                   # first element is a legal notice
        tags = {str(t).lower() for t in j.get("tags", []) or []}
        if want and not (tags & want):
            continue
        salary = ""
        if j.get("salary_min"):
            salary = f"USD {j['salary_min']:,}-{j.get('salary_max') or j['salary_min']:,}"
        out.append(RawJob(
            source="remoteok", external_id=str(j.get("id", "")), company=j.get("company", ""),
            title=j.get("position", ""), location=j.get("location") or "Remote",
            url=j.get("url", ""), apply_url=j.get("apply_url") or j.get("url", ""),
            description=html_to_text(j.get("description")), remote=True, salary=salary,
            posted_at=to_iso(j.get("epoch") or j.get("date"))))
    log.info("remoteok: %d jobs after tag filter", len(out))
    return out


def collect_wwr(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    feeds = cfg.section("sources", "weworkremotely", "feeds", default=[]) or []
    out = []
    with http_client() as client:
        for feed in feeds:
            try:
                r = client.get(feed)
                r.raise_for_status()
            except Exception as e:
                log.warning("wwr feed failed %s: %s", feed, e)
                continue
            save_raw(raw_dir, f"wwr_{feed.rsplit('/', 1)[-1]}", r.text)
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                raw_title = (item.findtext("title") or "").strip()
                company, _, title = raw_title.partition(":")
                if not title:
                    company, title = "", raw_title
                region = (item.findtext("region") or "Remote").strip()
                link = (item.findtext("link") or "").strip()
                out.append(RawJob(
                    source="weworkremotely", external_id=(item.findtext("guid") or link),
                    company=company.strip(), title=title.strip(), location=f"Remote ({region})",
                    url=link, apply_url=link, description=html_to_text(item.findtext("description")),
                    remote=True, posted_at=to_iso(item.findtext("pubDate"))))
    log.info("weworkremotely: %d jobs", len(out))
    return out
