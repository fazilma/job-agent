from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("jobagent.collect")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobagent/0.1"


def http_client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr)[^>]*>", re.I)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)            # Greenhouse double-escapes its HTML
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def pick(d: dict, *keys: str, default: Any = "") -> Any:
    """First non-empty value among keys. Keys may be dotted: 'company.name'."""
    for key in keys:
        node: Any = d
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if node not in (None, "", [], {}):
            return node
    return default


def to_iso(value: Any) -> str:
    """Best-effort date normalisation: epoch secs/ms or ISO-ish string -> YYYY-MM-DD."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            if v > 1e12:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
        s = str(value)
        m = re.match(r"\d{4}-\d{2}-\d{2}", s)
        if m:
            return m.group(0)
        m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", s)
        if m:   # shortcut: assumes day first (Gulf sites), so an ambiguous US date reads wrong; upgrade if a US source needs it
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


def save_raw(raw_dir: Path | None, name: str, payload: Any) -> None:
    if raw_dir is None:
        return
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:80]
    (raw_dir / f"{stamp}_{safe}.json").write_text(json.dumps(payload, default=str)[:5_000_000])
