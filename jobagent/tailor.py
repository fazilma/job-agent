"""Tailor agent: runs the anthropic-skills:tailor-resume skill headlessly, one job at a time."""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import claude_cli, db, impact

log = logging.getLogger("jobagent.tailor")

# What the skill needs: its own Skill load, file edits in the Resume root, and
# xelatex / poppler for compile + checks. No Agent/Task: a subagent re-reads
# everything in a second context, which roughly doubles the cost of each resume.
DEFAULT_TOOLS = [
    "Skill", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
    "Bash(xelatex:*)", "Bash(pdfinfo:*)", "Bash(pdftotext:*)", "Bash(pdffonts:*)",
    "Bash(mdls:*)",
    "Bash(cd:*)", "Bash(mkdir:*)", "Bash(cp:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)",
    "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(which:*)", "Bash(command -v:*)",
]
# Nobody is watching a background run, so the skill must not stop to ask.
BLOCKED_TOOLS = ["AskUserQuestion"]

EXTRA_LABELS = {
    "gap_analysis": "Gap analysis (Gap_Analysis.md)",
    "outreach_note": "Outreach note (Outreach_Note.md)",
    "cover_letter": "Cover letter (appended to Outreach_Note.md)",
}

PROMPT = """Use the {skill} skill to tailor Fazil's resume for the job below, with the
{impact_skill} skill's impact pass built in.

This is an unattended run from his job tracker. Nobody can answer questions, so:
- Scope is already decided: {scope}. No additions or highlights. Do not ask.
- The job folder is `{folder}` at the Resume root (use it instead of deriving a slug). It already
  exists. If it has older generated files, overwrite them.
- Keep it lean: do NOT spawn subagents (Agent/Task tools are unavailable). Do the LaTeX, compile
  and checks yourself in this session. Read each base file once.
- Where either skill says to stop and ask Fazil (e.g. still overflowing one page after all trims),
  make the most conservative choice that keeps every non-fabrication rule, and say what you did
  in the report.

## Order of work (every tool call costs Fazil's Claude usage: use as few as you can)
1. {jd_step}
2. Decide the content, in your head (tailor-resume steps 3-5): keywords, Summary, Skills order,
   which bullets to keep and in what order. Every claim must trace to the base resume. Do NOT
   write the tailored Markdown file and do NOT write a source-check table: go straight to LaTeX.
3. Impact pass on that content before writing it (resume-impact-pass steps 3 and 5,
   no-questions mode; skip its snapshot step, this resume is new):
   - Audit every kept bullet (strong / duty / weak wording).
   - Weak wording: reword only. Duty bullets: improve them only with facts from the base resume
     or from "Confirmed by Fazil" below. Never invent or estimate a number.
   - \\textbf only short measurable-impact spans; no bold on technology names; no keyword stuffing.
4. Write the tailored .tex in ONE Write call, named `Fazil_<CompanyOrFocus>.tex` in the job folder:
   the base .tex with only the content changed (never the preamble, geometry or custom commands),
   LaTeX specials escaped. Then compile once (xelatex -jobname=Fazil_Resume; twice only if needed).
5. Check in ONE Bash call: `pdfinfo Fazil_Resume.pdf; pdffonts Fazil_Resume.pdf; pdftotext
   Fazil_Resume.pdf -`. Check page count (one page; if over, trim per tailor-resume step 8 with
   Edit and recompile), contact details, headings, keywords, ligatures, split words, metrics,
   fonts embedded + unicode. Fix content issues; report preamble issues only. Don't read the
   xelatex .log unless the compile failed. NO image rendering (no pdftoppm, no PNGs).
6. Skip everything else in the skills for this run: no verification scorecard (no
   Verification.md, no Tailoring_Log.md row), no tailored .md, no report files. Do NOT edit
   Backend_2026/ files. Your reply is saved as skill_report.md automatically.
7. Reply: a short report (folder, what was emphasised, keyword coverage line, check results),
   then the blocks below.

## Confirmed by Fazil (answers from Impact_Questions.md; treat as verified facts)
{confirmed}

## Questions already on file (do not ask these again)
{already_asked}

## Questions for Fazil
For up to 5 duty bullets you kept in this resume whose real impact is unknown, and that are not
already on file above, end your reply with this block (exact base-resume bullet text, 1-2 concrete
questions each, answerable from memory or a dashboard; output `[]` if none):

```impact-questions
[{{"role": "CoinRoutes", "bullet": "<exact bullet text from the base resume>", "questions": ["...", "..."]}}]
```
{answers_block}
The very last line of your reply must be exactly:
OUTPUT_DIR=<absolute path of the job's folder>

## Job
Company: {company}
Role: {title}
Location: {location}
Posting: {url}

### Job description (verbatim)
{description}
"""

JD_SAVED = ("Job_Description.md is already saved in the job folder (it's the job description below): "
            "don't write or read it again. Load the skill and read the base .md (content) and base .tex "
            "(format) once.")
JD_MISSING = ("No job description was captured: fetch the posting URL once and save its text verbatim "
              "to Job_Description.md in the job folder. Load the skill and read the base .md (content) "
              "and base .tex (format) once.")

ANSWERS_BLOCK = """
Also put paste-ready answers for application forms in your reply (not in a file), using only facts
from the base resume (the no-fabrication rule applies), hyphens not em-dashes, in this block:

```application-answers
## Why this company
<2-3 sentences, based only on the posting text>

## Why this role
<2-3 sentences>

## About you
<3 sentences>

## Most relevant achievement
<2-3 sentences, exact metric from the base resume>
```
"""

PROFILE_FIELDS = [("notice_period", "Notice period"), ("current_location", "Current location"),
                  ("work_authorization", "Work authorization"), ("expected_salary", "Expected salary"),
                  ("linkedin", "LinkedIn"), ("github", "GitHub")]


def _profile(cfg) -> dict:
    prof = cfg.section("application_profile", default={}) or {}
    return {k: (prof.get(k) or "[fill in]") for k, _ in PROFILE_FIELDS}


def extract_answers(text: str) -> str:
    m = re.search(r"```application-answers\s*\n(.*?)```", text or "", re.S)
    return m.group(1).strip() if m else ""


def answers_file(cfg, row, written: str) -> str:
    """application_answers.md: Claude's four short answers plus the fixed fields from config."""
    prof = _profile(cfg)
    fixed = "\n".join(f"- **{label}:** {prof[k]}" for k, label in PROFILE_FIELDS)
    return (f"# Application answers - {row['company']} - {row['title']}\n\n"
            f"{written}\n\n## Form fields\n{fixed}\n")


def prepare_folder(cfg, row) -> Path:
    """Create the job folder and save the JD, so Claude doesn't spend steps (and tokens) on it."""
    folder = cfg.path("resume_workspace") / folder_name(row)
    folder.mkdir(parents=True, exist_ok=True)
    desc = (row["description"] or "").strip()
    if desc:
        head = [f"# {row['title']} - {row['company']}", ""]
        if row["location"]:
            head.append(f"Location: {row['location']}")
        if row["apply_url"] or row["url"]:
            head.append(f"Posting: {row['apply_url'] or row['url']}")
        (folder / "Job_Description.md").write_text("\n".join(head) + "\n\n" + desc + "\n", encoding="utf-8")
    return folder


def slug(s: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:max_len].rstrip("-") or "job"


def folder_name(row) -> str:
    return f"{slug(row['company'], 30)}_{slug(row['title'], 40)}"


def build_prompt(cfg, row) -> str:
    tc = cfg.section("tailor", default={}) or {}
    extras = [EXTRA_LABELS[e] for e in tc.get("extras", []) or [] if e in EXTRA_LABELS]
    scope = "Tailored resume only" if not extras else "Tailored resume + " + ", ".join(extras)
    entries = impact.load(cfg)
    return PROMPT.format(
        # In Docker the skills are local copies, so their names have no "anthropic-skills:"
        # prefix; docker-compose sets these two variables.
        skill=os.environ.get("JOBAGENT_TAILOR_SKILL") or tc.get("skill", "anthropic-skills:tailor-resume"),
        impact_skill=(os.environ.get("JOBAGENT_IMPACT_SKILL")
                      or tc.get("impact_skill", "anthropic-skills:resume-impact-pass")),
        confirmed=impact.confirmed_facts_block(entries), already_asked=impact.already_asked_block(entries),
        scope=scope, folder=folder_name(row),
        jd_step=JD_SAVED if (row["description"] or "").strip() else JD_MISSING,
        answers_block=ANSWERS_BLOCK if tc.get("write_answers", True) else "",
        company=row["company"], title=row["title"],
        location=row["location"] or "", url=row["apply_url"] or row["url"] or "",
        description=(row["description"] or "(no description captured - open the posting URL above)")[:15000])


def _find_output(text: str, workspace: Path, expected: Path, started: float) -> Path | None:
    m = re.search(r"OUTPUT_DIR=(.+)", text)
    if m:
        p = Path(m.group(1).strip().strip("`'\""))
        if p.is_dir():
            return p
    if (expected / "Fazil_Resume.pdf").exists():
        return expected
    # fallback: newest resume PDF written during this run
    candidates = [p for p in workspace.rglob("Fazil_Resume.pdf") if p.stat().st_mtime >= started - 5]
    return max(candidates, key=lambda p: p.stat().st_mtime).parent if candidates else None


def tailor_one(conn, cfg, row, llm: Callable[..., str] | None = None, timeout: float | None = None) -> bool:
    tc = cfg.section("tailor", default={}) or {}
    workspace = cfg.path("resume_workspace")
    if not workspace.exists():
        db.set_tailor(conn, row["id"], "failed", error=f"resume folder not found: {workspace}")
        return False
    prepare_folder(cfg, row)
    prompt = build_prompt(cfg, row)
    db.set_tailor(conn, row["id"], "running")
    started = time.time()
    try:
        if llm:
            text = llm(prompt, cwd=workspace)
        else:
            text = claude_cli.run(
                prompt, model=tc.get("model", "sonnet"), cwd=workspace,
                timeout=int(timeout or tc.get("timeout_minutes", 25) * 60),
                extra_args=["--permission-mode", "acceptEdits",
                            "--allowedTools", *(tc.get("allowed_tools") or DEFAULT_TOOLS),
                            "--disallowedTools", *BLOCKED_TOOLS,
                            # Its text can come from any website, so no way to send your files out.
                            *(["WebFetch"] if '"web_search"' in (row["sources"] or "") else [])])
    except claude_cli.UsageLimitError as e:
        db.log_claude_usage(conn, "tailor", dict(claude_cli.last_usage, ok=False), row["id"])
        # Not this job's fault: put it back so the next run (or a click) retries it.
        db.set_tailor(conn, row["id"], "none", error=f"Claude usage limit reached - retry later. {e}"[:500])
        log.warning("Claude usage limit reached; stopping tailoring for now (%s)", e)
        raise
    except Exception as e:
        db.log_claude_usage(conn, "tailor", dict(claude_cli.last_usage, ok=False), row["id"])
        db.set_tailor(conn, row["id"], "failed", error=str(e)[:1000])
        log.warning("tailor failed for %s @ %s: %s", row["title"], row["company"], e)
        return False

    if not llm:
        db.log_claude_usage(conn, "tailor", claude_cli.last_usage, row["id"])
    out_dir = _find_output(text, workspace, workspace / folder_name(row), started)
    pdf = out_dir / "Fazil_Resume.pdf" if out_dir else None
    if not pdf or not pdf.exists():
        db.set_tailor(conn, row["id"], "failed", error="no Fazil_Resume.pdf produced\n" + text[-800:])
        return False
    answers = None
    written = extract_answers(text)
    if written:
        answers = answers_file(cfg, row, written)
        (out_dir / "application_answers.md").write_text(answers, encoding="utf-8")
    (out_dir / "skill_report.md").write_text(text, encoding="utf-8")   # keyword coverage etc.
    questions = impact.extract_questions(text)
    if questions:
        try:
            added = impact.record(cfg, questions, f"{row['company']} - {row['title']}")
            log.info("impact questions: %d new bullet(s) added to %s", added, impact.FILE_NAME)
        except Exception as e:                       # never fail a finished resume over this
            log.warning("could not update %s: %s", impact.FILE_NAME, e)
    db.set_tailor(conn, row["id"], "done", resume_dir=str(out_dir), resume_pdf=str(pdf), answers=answers)
    log.info("tailored %s @ %s -> %s", row["title"], row["company"], out_dir)
    return True


def tailor_top(conn, cfg, llm=None) -> int:
    tc = cfg.section("tailor", default={}) or {}
    # Jobs that failed only because the usage limit was hit get another go.
    # (Older versions stored the raw CLI output, cut off before the limit message.)
    conn.execute("""UPDATE jobs SET tailor_status='none'
                    WHERE tailor_status='failed' AND (tailor_error LIKE '%usage_limit_reached%'
                          OR tailor_error LIKE '%usage limit%' OR tailor_error LIKE '%session limit%'
                          OR tailor_error LIKE '{"duration_api_ms"%' OR tailor_error LIKE '%cache_read_input_tokens%')""")
    conn.commit()
    rows = db.jobs_to_tailor(conn, tc.get("threshold", 80), tc.get("max_per_run", 3))
    from . import timeouts
    timeout = timeouts.tailor_timeout(conn, cfg)          # a few times a usual tailoring
    done, timed_out = 0, 0
    for row in rows:
        try:
            ok = tailor_one(conn, cfg, row, llm=llm, timeout=timeout)
        except claude_cli.UsageLimitError:
            break                               # every further call would fail the same way
        done += ok
        err = "" if ok else (db.get_job(conn, row["id"])["tailor_error"] or "")
        timed_out = timed_out + 1 if "timed out" in err else 0
        if timed_out >= 2:                      # circuit breaker: Claude is stuck, try again next run
            log.warning("two tailorings in a row timed out (limit %.0fs); stopping tailoring for this run", timeout)
            break
    return done


# ---------------------------------------------------------------- dashboard queue
# Clicks on "Tailor" are queued and run one at a time, so ten clicks don't start
# ten Claude sessions at once.

_queue: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def enqueue(job_id: int, open_conn: Callable) -> None:
    global _worker
    _queue.put(job_id)
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, args=(open_conn,), daemon=True)
            _worker.start()


def _work(open_conn: Callable) -> None:
    while True:
        job_id = _queue.get()          # long-lived worker; blocks until the next click
        conn, cfg = open_conn()
        try:
            row = db.get_job(conn, job_id)
            if row and row["tailor_status"] == "queued":
                tailor_one(conn, cfg, row)
        except claude_cli.UsageLimitError:
            # Release everything still waiting so the dashboard doesn't show "queued" forever.
            while not _queue.empty():
                jid = _queue.get_nowait()
                db.set_tailor(conn, jid, "none", error="Claude usage limit reached - click Tailor again later")
        except Exception:
            log.exception("queued tailor failed for job %s", job_id)
        finally:
            conn.close()
