"""LinkedIn, Bayt and NaukriGulf through Apify actors.

Actor output fields vary between actors and versions, so mapping is tolerant:
each field tries several likely keys. If a source starts returning blank
titles/companies, check data/raw/ for the actual keys and add them below.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ..models import RawJob
from .base import html_to_text, pick, save_raw, to_iso

log = logging.getLogger("jobagent.collect.apify")


def _client():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set (add it to .env)")
    from apify_client import ApifyClient
    return ApifyClient(token)


def _run_field(run, snake: str, camel: str):
    """apify-client v1/v2 return a dict (camelCase); v3 returns a pydantic model (snake_case)."""
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get(camel) or run.get(snake)
    return getattr(run, snake, None)


def run_actor(actor: str, run_input: dict, timeout_secs: int = 240,
              max_charge_usd: float | None = None) -> list[dict]:
    import inspect
    from datetime import timedelta

    client = _client()
    actor_client = client.actor(actor)
    params = inspect.signature(actor_client.call).parameters
    if "run_timeout" in params:             # apify-client v3+
        kwargs = {"run_timeout": timedelta(seconds=timeout_secs),
                  "wait_duration": timedelta(seconds=timeout_secs + 30)}
        if "logger" in params:
            # v3 streams the actor's live log in a background thread; we don't use it,
            # and a dropped stream prints a harmless but alarming traceback.
            kwargs["logger"] = None
    else:                                   # apify-client v1/v2
        kwargs = {"timeout_secs": timeout_secs}
        if "wait_secs" in params:
            kwargs["wait_secs"] = timeout_secs + 30
    if max_charge_usd is not None and "max_total_charge_usd" in params:
        from decimal import Decimal
        # Pay-per-result actors stop by themselves once this run has cost this much.
        kwargs["max_total_charge_usd"] = Decimal(str(round(max_charge_usd, 4)))
    run = actor_client.call(run_input=run_input, **kwargs)
    dataset_id = _run_field(run, "default_dataset_id", "defaultDatasetId")
    if not dataset_id:
        raise RuntimeError(f"actor {actor} returned no dataset")
    status = str(_run_field(run, "status", "status") or "")
    if status and "SUCCEEDED" not in status.upper():
        log.warning("actor %s finished with status %s; using whatever it collected", actor, status)
    return list(client.dataset(dataset_id).iterate_items())


def map_item(source: str, it: dict) -> RawJob | None:
    title = pick(it, "title", "jobTitle", "position", "name")
    company = pick(it, "companyName", "company.name", "company", "employer", "hiringOrganization.name",
                   "organization")
    if isinstance(company, dict):
        company = pick(company, "name", default="")
    if not title or not company:
        return None
    location = pick(it, "location", "jobLocation", "formattedLocation", "city", "place")
    if isinstance(location, (list, dict)):
        location = ", ".join(location) if isinstance(location, list) else pick(location, "name", "city")
    url = pick(it, "jobUrl", "url", "link", "jobLink", "detailUrl")
    apply_url = pick(it, "applyUrl", "applyLink", "externalApplyUrl", "apply_url", "companyApplyUrl", default=url)
    desc = pick(it, "descriptionText", "description", "jobDescription", "descriptionMarkdown",
                "descriptionHtml", "content")
    desc = html_to_text(desc) if "<" in str(desc) else str(desc)
    remote_flag = pick(it, "isRemote", "remote", "workRemoteAllowed", default=False)
    work_type = str(pick(it, "workType", "workplaceType", "employmentType", "workplaceTypes")).lower()
    salary = pick(it, "salaryText", "salary", "salaryInfo", "salaryRange", "compensation")
    if isinstance(salary, (dict, list)):
        salary = str(salary)[:120]
    return RawJob(
        source=source,
        external_id=str(pick(it, "id", "jobId", "jobPostingId", "trackingId")),
        title=str(title).strip(), company=str(company).strip(), location=str(location or ""),
        url=str(url), apply_url=str(apply_url), description=desc,
        remote=bool(remote_flag) or "remote" in work_type or "remote" in str(location).lower(),
        salary=str(salary or ""),
        posted_at=to_iso(pick(it, "postedAt", "postedDate", "publishedAt", "datePosted", "listedAt",
                              "postedTime", "date")),
    )


class _Gate:
    """One limit on Apify searches running at once, shared by LinkedIn, Bayt and NaukriGulf
    (the free plan allows 5 at a time across the whole account). Waiting searches are let in
    by priority, so LinkedIn goes first."""

    def __init__(self, slots: int):
        import heapq, itertools, threading
        self._heapq, self._count = heapq, itertools.count()
        self.slots, self.used, self.waiting = slots, 0, []
        self.cond = threading.Condition()

    def acquire(self, priority: int) -> None:
        ticket = (priority, next(self._count))
        with self.cond:
            self._heapq.heappush(self.waiting, ticket)
            while not (self.used < self.slots and self.waiting[0] == ticket):
                self.cond.wait()
            self._heapq.heappop(self.waiting)
            self.used += 1
            self.cond.notify_all()

    def release(self) -> None:
        with self.cond:
            self.used -= 1
            self.cond.notify_all()


_gate: _Gate | None = None
_gate_lock = __import__("threading").Lock()
PRIORITY = {"linkedin": 0, "bayt": 1, "naukrigulf": 2}


def _shared_gate(slots: int) -> _Gate:
    global _gate
    with _gate_lock:
        if _gate is None or _gate.slots != slots:
            _gate = _Gate(slots)
        return _gate


def _is_concurrency_limit(e: Exception) -> bool:
    return "concurrent actor runs" in str(e).lower()


def _collect(source: str, actor: str, inputs: list[dict], raw_dir: Path | None,
             max_seconds: int = 240, parallel_runs: int = 4,
             max_charge_usd: float | None = None, max_concurrent: int = 4) -> list[RawJob]:
    """Each search is a separate run on Apify's servers, so several can run at once;
    we just wait for them. A search that overruns is stopped by Apify at max_seconds."""
    import time as _time
    from .. import parallel
    gate = _shared_gate(max_concurrent)
    prio = PRIORITY.get(source, 9)

    def one(run_input: dict, label: str) -> list[RawJob]:
        if prio:
            _time.sleep(1.0 * prio)                 # let LinkedIn's searches queue up first
        for attempt in range(4):
            gate.acquire(prio)
            try:
                items = run_actor(actor, run_input, timeout_secs=max_seconds, max_charge_usd=max_charge_usd)
                break
            except Exception as e:
                # Apify is full (e.g. searches still running from an earlier run): wait, try again.
                if not _is_concurrency_limit(e) or attempt == 3:
                    raise
                log.info("%s '%s': Apify is at its limit of runs at once; retrying in %ds", source, label,
                         20 * (attempt + 1))
            finally:
                gate.release()
            _time.sleep(20 * (attempt + 1))
        save_raw(raw_dir, f"{source}_{label}", items)
        mapped = [m for m in (map_item(source, it) for it in items) if m]
        if items and not mapped:
            log.warning("%s: %d items but none mapped; check field names in data/raw", source, len(items))
        log.info("%s '%s': %d jobs", source, label, len(mapped))
        return mapped

    tasks = {}
    for run_input in inputs:
        label = " ".join(str(v) for v in run_input.values() if isinstance(v, str))
        tasks[label] = (lambda ri=run_input, lb=label: one(ri, lb))
    out: list[RawJob] = []
    for label, (res, secs) in parallel.run_all(tasks, max_workers=parallel_runs,
                                               deadline_s=max_seconds * 2 + 120).items():
        if isinstance(res, BaseException):
            log.warning("%s search failed (%s) after %.0fs: %s", source, label, secs, res)
        else:
            out.extend(res)
    return out


def _limits(cfg) -> dict:
    a = cfg.section("apify", default={}) or {}
    return {"max_seconds": int(a.get("max_seconds_per_search", 240)),
            "parallel_runs": int(a.get("parallel_runs", 4)),
            "max_concurrent": int(a.get("max_concurrent_runs", 4))}


# ---------------------------------------------------------------- monthly budget

# Pay-per-event prices of the actors we use (free tier), so a run can be sized to fit.
PRICES = {
    "linkedin": {"per_result": 0.0004, "per_start": 0.001},     # valig/linkedin-jobs-scraper
    "bayt": {"per_result": 0.00099, "per_start": 0.0005},       # blackfalcondata/bayt-scraper
    "naukrigulf": {"per_result": 0.001, "per_start": 0.005},    # blackfalcondata/naukrigulf-scraper
}

_usage_cache: dict = {}


def account_usage() -> dict | None:
    """Spend so far this cycle, from Apify itself. None if it can't be read."""
    if "v" in _usage_cache:
        return _usage_cache["v"]
    val = None
    try:
        lim = _client().user().limits()
        if isinstance(lim, dict):                        # apify-client v1/v2
            cur, mx, cyc = lim.get("current", {}), lim.get("limits", {}), lim.get("monthlyUsageCycle", {})
            val = {"used": float(cur.get("monthlyUsageUsd") or 0), "limit": float(mx.get("maxMonthlyUsageUsd") or 0),
                   "cycle_end": str(cyc.get("endAt") or "")}
        else:                                            # v3: pydantic model
            val = {"used": float(lim.current.monthly_usage_usd or 0),
                   "limit": float(lim.limits.max_monthly_usage_usd or 0),
                   "cycle_end": str(lim.monthly_usage_cycle.end_at or "")}
    except Exception as e:
        log.warning("couldn't read Apify usage (%s); using the safe fallback cap", e)
    _usage_cache["v"] = val
    return val


def budget_plan(cfg, usage: dict | None = None) -> dict:
    """How much each source may spend in this run.

    allowance today = (budget - spent - reserve) / days left in the billing cycle,
    split between sources by `share`, then evenly between that source's searches.
    """
    from datetime import datetime, timezone

    a = cfg.section("apify", default={}) or {}
    usage = usage if usage is not None else account_usage()
    shares = a.get("share") or {"linkedin": 0.8, "bayt": 0.1, "naukrigulf": 0.1}
    fallback = float(a.get("fallback_usd_per_search", 0.015))
    searches = {src: len((cfg.section("sources", src, default={}) or {}).get("searches", []))
                for src in shares if (cfg.section("sources", src, default={}) or {}).get("enabled", True)}
    if not usage:
        return {"known": False, "per_search": {s: fallback for s in searches}, "today": None}
    budget = float(a.get("monthly_budget_usd") or 0) or usage["limit"] or 5.0
    if usage["limit"]:
        budget = min(budget, usage["limit"])
    left = budget - usage["used"] - float(a.get("reserve_usd", 0.25))
    try:
        end = datetime.fromisoformat(usage["cycle_end"].replace("Z", "+00:00"))
        days = max(1.0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        days = 30.0
    today = max(0.0, left / days)
    total_share = sum(shares[s] for s in searches) or 1
    per_search = {s: (today * shares[s] / total_share) / n if n else 0.0 for s, n in searches.items()}
    return {"known": True, "used": usage["used"], "budget": budget, "left": left, "days": days,
            "today": today, "per_search": per_search, "cycle_end": usage["cycle_end"]}


def results_for(source: str, usd: float, configured: int) -> int:
    """How many results fit in `usd` for one search, never more than configured."""
    p = PRICES[source]
    return max(0, min(configured, int((usd - p["per_start"]) / p["per_result"] + 1e-6)))


def _sized(cfg, source: str, configured: int) -> tuple[int, float | None]:
    plan = budget_plan(cfg)
    usd = plan["per_search"].get(source, 0.0)
    n = results_for(source, usd, configured)
    if plan.get("known"):
        log.info("%s: Apify budget %.2f/%.2f USD used, %.1f days left -> %.3f USD/search, up to %d results",
                 source, plan["used"], plan["budget"], plan["days"], usd, n)
    return n, usd


def _state_key(*parts: str) -> str:
    import re
    return "jobagent-" + re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")[:60]


def known_linkedin_ids(cfg, max_ids: int = 5000) -> list[str]:
    """LinkedIn job IDs already in the tracker, newest first. Passed to the scraper as
    skipJobId, so a wider date window doesn't mean paying again for jobs you have."""
    import re
    import sqlite3
    p = cfg.path("db")
    if not p.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        rows = conn.execute("SELECT url FROM jobs WHERE url LIKE '%linkedin.com/jobs/view/%' "
                            "ORDER BY id DESC LIMIT ?", (max_ids,)).fetchall()
        conn.close()
    except Exception:
        return []
    ids = []
    for (url,) in rows:
        m = re.search(r"/jobs/view/\S*?(\d{6,})/?(?:[?#].*)?$", url or "")
        if m:
            ids.append(m.group(1))
    return ids


def collect_linkedin(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    c = cfg.section("sources", "linkedin")
    limit, cap = _sized(cfg, "linkedin", int(c.get("limit_per_search", 150)))
    if limit < 5:
        log.warning("linkedin: skipped, Apify budget for this cycle is used up")
        return []
    skip = known_linkedin_ids(cfg) if c.get("skip_known", True) else []
    if skip:
        log.info("linkedin: skipping %d jobs already in the tracker", len(skip))
    inputs = []
    for s in c.get("searches", []):
        run_input = {"keywords": s["keywords"], "location": s["location"],
                     "datePosted": c.get("date_posted", "r604800"), "limit": limit}
        if skip:
            run_input["skipJobId"] = skip
        inputs.append(run_input)
    return _collect("linkedin", c["actor"], inputs, raw_dir, max_charge_usd=cap, **_limits(cfg))


def collect_bayt(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    c = cfg.section("sources", "bayt")
    limit, cap = _sized(cfg, "bayt", int(c.get("max_results", 40)))
    if limit < 3:
        log.warning("bayt: skipped, Apify budget for this cycle is used up")
        return []
    # Past week + incremental mode: the actor remembers what it returned before and
    # only returns (and charges for) new or changed jobs.
    inputs = [{
        "query": s["query"], "country": s.get("country", "AE"), "datePosted": c.get("date_posted", "past-week"),
        "sortBy": "date", "maxResults": limit, "includeDetails": True,
        "descriptionFormat": "text", "incrementalMode": bool(c.get("incremental", True)),
        # fixed memory name: the daily result limit changes with the budget, and an
        # auto-derived key would then forget what was already returned
        "stateKey": _state_key("bayt", s["query"], s.get("country", "AE")),
    } for s in c.get("searches", [])]
    return _collect("bayt", c["actor"], inputs, raw_dir, max_charge_usd=cap, **_limits(cfg))


def collect_naukrigulf(cfg, raw_dir: Path | None = None) -> list[RawJob]:
    c = cfg.section("sources", "naukrigulf")
    limit, cap = _sized(cfg, "naukrigulf", int(c.get("max_results", 40)))
    if limit < 3:
        log.warning("naukrigulf: skipped, Apify budget for this cycle is used up")
        return []
    inputs = [{
        "query": s["query"], "location": s.get("location", "UAE"), "datePosted": int(c.get("days_posted", 7)),
        "maxResults": limit, "includeDetails": True, "incrementalMode": bool(c.get("incremental", True)),
        "stateKey": _state_key("naukrigulf", s["query"], s.get("location", "UAE")),
    } for s in c.get("searches", [])]
    return _collect("naukrigulf", c["actor"], inputs, raw_dir, max_charge_usd=cap, **_limits(cfg))
