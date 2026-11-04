"""Local dashboard: tabs over one jobs table, plus Apply / Mark applied / Skip actions."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import db, tailor
from .config import ROOT, load_config
from .models import ACTIVE, APPLIED, CLOSED, INTERVIEWING, OPENED, SKIPPED

app = FastAPI(title="Job agent")
TEMPLATE = Path(__file__).parent / "templates" / "index.html"
JD_TEMPLATE = Path(__file__).parent / "templates" / "jd.html"
_run_proc: subprocess.Popen | None = None
_lock = threading.Lock()

ALLOWED_STATUS = {ACTIVE, OPENED, APPLIED, INTERVIEWING, SKIPPED, CLOSED}


def _conn():
    cfg = load_config()
    return db.connect(cfg.path("db")), cfg


def _job_dict(r, follow: set[int]) -> dict:
    d = dict(r)
    for k in ("sources", "score_reasons", "red_flags"):
        try:
            d[k] = json.loads(d[k]) if d[k] else []
        except (TypeError, json.JSONDecodeError):
            d[k] = []
    d.pop("description", None)           # fetched on demand in details
    d["follow_up"] = r["id"] in follow
    d["has_pdf"] = bool(r["resume_pdf"] and Path(r["resume_pdf"]).exists())
    return d


# Finder only exists when the dashboard runs directly on the Mac. In Docker the dashboard
# links to the PDF instead (and the file sits in the same Resume folder on the Mac).
CAN_REVEAL = sys.platform == "darwin"


def _reveal(path: str) -> bool:
    """Show the resume in Finder so it can be dragged into an upload form."""
    if CAN_REVEAL and path and Path(path).exists():
        subprocess.Popen(["open", "-R", path])
        return True
    return False


@app.get("/", response_class=HTMLResponse)
def index():
    return TEMPLATE.read_text(encoding="utf-8")


@app.get("/api/jobs")
def jobs(tab: str = "active", q: str = "", min_score: int | None = None, include_filtered: bool = False,
         resume_ready: bool = False, gigs_only: bool = False):
    if tab not in db.TAB_QUERIES:
        raise HTTPException(400, "unknown tab")
    conn, cfg = _conn()
    try:
        follow = db.follow_ups(conn, cfg.section("tracking", "follow_up_after_days", default=7))
        rows = db.list_tab(conn, tab, q=q, min_score=min_score, include_filtered=include_filtered,
                           resume_ready=resume_ready, gigs_only=gigs_only)
        return {"jobs": [_job_dict(r, follow) for r in rows], "counts": db.tab_counts(conn),
                "pending": [{"id": r["id"], "title": r["title"], "company": r["company"]}
                            for r in db.pending_confirmations(conn)],
                "threshold": cfg.section("tailor", "threshold", default=70),
                "last_run": dict(db.latest_run(conn) or {}) or None,
                "running": _run_proc is not None and _run_proc.poll() is None,
                "can_reveal": CAN_REVEAL}
    finally:
        conn.close()


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    conn, cfg = _conn()
    try:
        r = db.get_job(conn, job_id)
        if not r:
            raise HTTPException(404)
        d = _job_dict(r, set())
        d["description"] = r["description"]
        d["history"] = [dict(h) for h in db.history(conn, job_id)]
        return d
    finally:
        conn.close()


class StatusIn(BaseModel):
    status: str
    reason: str | None = None


@app.post("/api/jobs/{job_id}/status")
def set_status(job_id: int, body: StatusIn):
    if body.status not in ALLOWED_STATUS:
        raise HTTPException(400, "bad status")
    conn, _ = _conn()
    try:
        db.set_status(conn, job_id, body.status, body.reason)
        return {"ok": True}
    except KeyError:
        raise HTTPException(404)
    finally:
        conn.close()


@app.post("/api/jobs/{job_id}/apply")
def apply(job_id: int):
    """Browser opens the posting itself; here we mark it opened and reveal the PDF."""
    conn, _ = _conn()
    try:
        r = db.get_job(conn, job_id)
        if not r:
            raise HTTPException(404)
        if r["status"] == ACTIVE:
            db.set_status(conn, job_id, OPENED, "clicked Apply")
        return {"ok": True, "revealed": _reveal(r["resume_pdf"]), "resume_pdf": r["resume_pdf"]}
    finally:
        conn.close()


class AddIn(BaseModel):
    url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""


def _score_in_background(job_id: int) -> None:
    def work():
        conn, cfg = _conn()
        try:
            from . import scorer
            scorer.score_pending(conn, cfg, job_ids=[job_id])
        except Exception:
            import logging
            logging.getLogger("jobagent.web").exception("scoring job %s added by link failed", job_id)
        finally:
            conn.close()
    threading.Thread(target=work, daemon=True, name=f"score-{job_id}").start()


@app.post("/api/jobs/add")
def add_job(body: AddIn):
    """Add a job by its link. With title + company filled in, those are used as-is (for pages
    that can't be read); otherwise the link is fetched."""
    from . import addlink
    from .models import RawJob
    if body.title.strip() and body.company.strip():
        url = body.url.strip()
        raw = RawJob(source=addlink.source_for(url) if url else "manual", title=body.title.strip(),
                     company=body.company.strip(), location=body.location.strip(), url=url, apply_url=url,
                     description=body.description.strip(), remote="remote" in body.location.lower())
        li = addlink.linkedin_id(url)
        if li:
            raw.url = raw.apply_url = f"https://www.linkedin.com/jobs/view/{li}/"
    elif body.url.strip():
        try:
            raw = addlink.fetch(body.url)
        except addlink.FetchError as e:
            return {"ok": False, "need_details": True, "error": str(e)}
    else:
        raise HTTPException(400, "paste a job link")
    conn, cfg = _conn()
    try:
        res = addlink.add(conn, cfg, raw)
    finally:
        conn.close()
    if not res["scored"]:
        _score_in_background(res["id"])
    return {"ok": True, **res, "scoring": not res["scored"]}


class NotesIn(BaseModel):
    notes: str


@app.post("/api/jobs/{job_id}/notes")
def notes(job_id: int, body: NotesIn):
    conn, _ = _conn()
    try:
        db.set_notes(conn, job_id, body.notes)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/jobs/{job_id}/tailor")
def tailor_now(job_id: int):
    """Works for any job, including ones the filter dropped. Runs one at a time."""
    conn, _ = _conn()
    try:
        r = db.get_job(conn, job_id)
        if not r:
            raise HTTPException(404)
        if r["tailor_status"] in ("running", "queued"):
            return {"ok": True, "already": True}
        db.set_tailor(conn, job_id, "queued")
    finally:
        conn.close()
    tailor.enqueue(job_id, _conn)
    return {"ok": True}


@app.get("/jobs/{job_id}/jd", response_class=HTMLResponse)
def jd_page(job_id: int):
    """The job description as captured, on its own page."""
    conn, _ = _conn()
    try:
        r = db.get_job(conn, job_id)
        if not r:
            raise HTTPException(404)
        return JD_TEMPLATE.read_text(encoding="utf-8").replace("__JOB_ID__", str(job_id))
    finally:
        conn.close()


@app.get("/files/resume/{job_id}")
def resume_pdf(job_id: int):
    conn, _ = _conn()
    try:
        r = db.get_job(conn, job_id)
        if not r or not r["resume_pdf"] or not Path(r["resume_pdf"]).exists():
            raise HTTPException(404)
        return FileResponse(r["resume_pdf"], media_type="application/pdf")
    finally:
        conn.close()


@app.post("/api/jobs/{job_id}/reveal")
def reveal(job_id: int):
    conn, _ = _conn()
    try:
        r = db.get_job(conn, job_id)
        if not r:
            raise HTTPException(404)
        return {"ok": True, "revealed": _reveal(r["resume_pdf"]), "resume_pdf": r["resume_pdf"]}
    finally:
        conn.close()


RUN_MODES = {
    "full": [],                                    # collect, score, tailor (what the schedule runs)
    "no_tailor": ["--no-tailor"],                  # collect + score, no Claude tailoring
    "collect": ["--no-score", "--no-tailor"],      # collect only, no Claude at all
}
_run_info: dict = {}


def start_run(log_name: str = "manual_run.log", mode: str = "full") -> bool:
    """Start `jobagent run` in its own process. False if a run is already going."""
    global _run_proc
    with _lock:
        if _run_proc is not None and _run_proc.poll() is None:
            return False
        (ROOT / "logs").mkdir(exist_ok=True)
        log = open(ROOT / "logs" / log_name, "a")
        log.write(f"\n===== {datetime.now().astimezone():%Y-%m-%d %H:%M} run ({mode}) =====\n")
        log.flush()
        _run_proc = subprocess.Popen([sys.executable, "-m", "jobagent.cli", "run", *RUN_MODES[mode]], cwd=ROOT,
                                     stdout=log, stderr=subprocess.STDOUT)
        _run_info.update(log=log_name, mode=mode, started=datetime.now().astimezone().isoformat())
        return True


class RunIn(BaseModel):
    mode: str = "full"


@app.post("/api/run")
def run_now(body: RunIn | None = None):
    """Kick off the same pipeline the schedule runs, in a separate process."""
    mode = (body.mode if body else "full")
    if mode not in RUN_MODES:
        raise HTTPException(400, "unknown mode")
    started = start_run(mode=mode)
    return {"ok": True, "already": not started}


# ---------------------------------------------------------------- System page

SYSTEM_TEMPLATE = Path(__file__).parent / "templates" / "system.html"

# Maintenance commands the System page can start (one at a time). Same as `jobagent <cmd>`.
TASKS = {
    "refilter": "Re-apply the filter rules in config.yaml to jobs already collected",
    "rescore": "Re-score every Active job (uses Claude: Haiku)",
    "compare-scores": "Re-score the 25 latest jobs with Sonnet and compare with Haiku (saves nothing; about $0.40)",
    "check-companies": "Check the career-page tokens in config.yaml",
    "check-agencies": "Test the recruitment-agency scrapers (saves nothing)",
    "apify-usage": "Apify spend this cycle and today's budget",
}
_task: dict = {"name": None, "proc": None}
LOGS = {"scheduled": "scheduled_run.log", "manual": "manual_run.log", "app": "jobagent.log"}


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400_000))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def _schedule_info(cfg, conn) -> dict:
    from . import daily
    import os
    now = datetime.now().astimezone()
    tz = os.environ.get("TZ") or now.tzname()
    row = conn.execute("SELECT MAX(started_at) AS s FROM runs").fetchone()
    last_start = datetime.fromisoformat(row["s"]).astimezone() if row and row["s"] else None
    if _run_proc is not None and _run_proc.poll() is None:
        last_start = now                    # a run is going: today's is covered, next is tomorrow
    if daily.STATE["enabled"]:
        at = daily.STATE["at"]
        nxt = daily.next_run(now, at, last_start, daily.STATE["last_attempt"])
        return {"kind": "docker", "at": at.strftime("%H:%M"), "tz": tz, "next_run": nxt.isoformat(),
                "last_check": daily.STATE["last_check"].isoformat() if daily.STATE["last_check"] else None}
    at = daily.launchd_time()
    if at:
        nxt = daily.next_run(now, daily.parse_at(at), last_start, None)
        return {"kind": "launchd", "at": at, "tz": tz, "next_run": nxt.isoformat()}
    return {"kind": "none", "tz": tz}


AUTH_LABELS = {
    "oauth_token": "long-lived token (CLAUDE_CODE_OAUTH_TOKEN)",
    "claude.ai": "claude.ai login",
    "api_key": "API key (ANTHROPIC_API_KEY, billed separately from your plan)",
    "api_key_helper": "API key helper",
    "third_party": "cloud provider",
}
_auth_cache: dict = {}


def _claude_login(conn) -> dict:
    """Is Claude Code set up to log in? `claude auth status` only checks that a credential is
    present (a wrong token still says logged in), so the last successful call is shown too."""
    import time as _time
    from . import claude_cli
    info = _auth_cache.get("v")
    if not info or _time.time() - _auth_cache.get("t", 0) > 60:
        try:
            out = subprocess.run([claude_cli.claude_bin(), "auth", "status"], capture_output=True, text=True,
                                 timeout=20, cwd=ROOT)
            st = json.loads(out.stdout or "{}")
            method = st.get("authMethod") or "none"
            info = {"installed": True, "logged_in": bool(st.get("loggedIn")) and method != "none",
                    "method": method, "label": AUTH_LABELS.get(method, method)}
        except claude_cli.ClaudeError:
            info = {"installed": False, "logged_in": False, "method": "none", "label": "Claude Code not found"}
        except Exception as e:
            info = {"installed": True, "logged_in": False, "method": "unknown", "label": f"couldn't check: {e}"[:200]}
        _auth_cache.update(v=info, t=_time.time())
    last_ok = conn.execute("SELECT at, model FROM claude_usage WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    last_fail = conn.execute("SELECT at FROM claude_usage WHERE ok=0 ORDER BY id DESC LIMIT 1").fetchone()
    return {**info, "last_ok": dict(last_ok) if last_ok else None, "last_fail": last_fail["at"] if last_fail else None}


@app.post("/api/system/claude-test")
def claude_test():
    """One tiny Haiku call, to prove the login really works."""
    from . import claude_cli
    conn, _ = _conn()
    try:
        try:
            reply = claude_cli.run("Reply with exactly: ok", model="haiku", timeout=90, extra_args=["--tools", ""])
            ok, msg = True, (reply or "").strip()[:200]
        except Exception as e:
            ok, msg = False, str(e)[:400]
        if claude_cli.last_usage:
            db.log_claude_usage(conn, "test", dict(claude_cli.last_usage, ok=ok))
        _auth_cache.clear()
        return {"ok": ok, "message": msg}
    finally:
        conn.close()


@app.get("/system", response_class=HTMLResponse)
def system_page():
    return SYSTEM_TEMPLATE.read_text(encoding="utf-8")


@app.get("/api/system")
def system_info(days: int = 14):
    conn, cfg = _conn()
    try:
        runs = []
        for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30"):
            d = dict(r)
            try:
                d["stats"] = json.loads(d["stats"]) if d["stats"] else {}
            except json.JSONDecodeError:
                d["stats"] = {}
            runs.append(d)
        tailor_states = {r["tailor_status"]: r["n"] for r in conn.execute(
            "SELECT tailor_status, COUNT(*) AS n FROM jobs GROUP BY tailor_status")}
        failed = [dict(r) for r in conn.execute(
            """SELECT id, company, title, tailor_error FROM jobs WHERE tailor_status='failed'
               ORDER BY last_seen DESC LIMIT 10""")]
        try:
            from . import impact
            entries = impact.load(cfg)
            questions = {"waiting": sum(1 for e in entries if not e.answered and not e.skipped),
                         "answered": sum(1 for e in entries if e.answered),
                         "skipped": sum(1 for e in entries if e.skipped), "file": str(impact.path(cfg))}
        except Exception:
            questions = None
        running = _run_proc is not None and _run_proc.poll() is None
        return {
            "now": datetime.now().astimezone().isoformat(),
            "schedule": _schedule_info(cfg, conn),
            "running": running, "run": dict(_run_info) if running else None,
            "runs": runs,
            "usage": [dict(r) for r in db.claude_usage_by_day(conn, days)],
            "costliest": [dict(r) for r in db.costliest_tailorings(conn, days)],
            "tailor": {"states": tailor_states, "failed": failed,
                       "model": cfg.section("tailor", "model", default="sonnet"),
                       "threshold": cfg.section("tailor", "threshold", default=80),
                       "max_per_run": cfg.section("tailor", "max_per_run", default=3)},
            "scorer_model": cfg.section("scorer", "model", default="haiku"),
            "login": _claude_login(conn),
            "questions": questions,
            "tasks": TASKS,
            "task": _task_status(),
        }
    finally:
        conn.close()


@app.get("/api/system/apify")
def system_apify():
    """Live Apify spend and today's budget (asks Apify, so it's loaded on request)."""
    from .collectors import apify_boards as ab
    cfg = load_config()
    u = ab.account_usage()
    if not u:
        return {"ok": False, "error": "Couldn't read Apify usage (check APIFY_TOKEN and the connection)."}
    plan = ab.budget_plan(cfg, u)
    searches = {}
    for src, usd in plan["per_search"].items():
        c = cfg.section("sources", src, default={}) or {}
        conf = int(c.get("limit_per_search") or c.get("max_results") or 40)
        searches[src] = {"usd": usd, "results": ab.results_for(src, usd, conf),
                         "searches": len(c.get("searches", []))}
    return {"ok": True, "used": plan["used"], "budget": plan["budget"], "left": max(plan["left"], 0),
            "days": plan["days"], "today": plan["today"], "cycle_end": plan["cycle_end"], "sources": searches}


@app.get("/api/logs/{name}")
def logs(name: str, lines: int = 200):
    if name not in LOGS:
        raise HTTPException(404)
    return {"name": LOGS[name], "text": _tail(ROOT / "logs" / LOGS[name], min(max(lines, 10), 2000))}


def _task_status() -> dict:
    p = _task["proc"]
    return {"name": _task["name"], "running": p is not None and p.poll() is None,
            "exit": None if p is None else p.poll()}


@app.post("/api/tasks/{name}")
def start_task(name: str):
    if name not in TASKS:
        raise HTTPException(404)
    with _lock:
        if _task["proc"] is not None and _task["proc"].poll() is None:
            return {"ok": False, "busy": _task["name"]}
        (ROOT / "logs").mkdir(exist_ok=True)
        log = open(ROOT / "logs" / "task.log", "w")
        log.write(f"$ jobagent {name}    ({datetime.now().astimezone():%Y-%m-%d %H:%M})\n\n")
        log.flush()
        _task.update(name=name, proc=subprocess.Popen([sys.executable, "-m", "jobagent.cli", name], cwd=ROOT,
                                                      stdout=log, stderr=subprocess.STDOUT))
    return {"ok": True}


@app.get("/api/tasks")
def task_output():
    return {**_task_status(), "text": _tail(ROOT / "logs" / "task.log", 400)}
