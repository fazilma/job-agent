"""Scorer agent: rates each job 0-100 against the base resume, in batches."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from . import claude_cli, db

log = logging.getLogger("jobagent.score")

PROMPT = """You are screening job postings for one candidate. Score how well each job fits him.

## Candidate resume
{resume}

## What he wants
- Roles: any software engineering role - backend, full-stack, software engineer/developer,
  forward deployed engineer, implementation engineer - at senior level or above.
- Business domain does NOT matter. A feature-flag SaaS, e-commerce, health-tech or dev-tools
  company is just as good as fintech or trading. Never lower a score, and never add a red flag,
  because the company is outside fintech/trading.
- What matters: how much of the required tech stack and engineering experience his resume
  actually covers (languages, frameworks, distributed systems, scale), seniority, and location.
- Locations: UAE (Dubai/Abu Dhabi) on-site or hybrid; fully remote roles open to someone in Dubai;
  or abroad only with visa sponsorship / relocation.
- Seniority: senior or above (10+ years). Junior/mid roles score low.
{feedback}
## Scoring guide
90-100 near-perfect: stack, engineering experience, seniority and location all match.
70-89 strong: worth a tailored resume.
50-69 partial: notable gaps in required stack, seniority or location.
0-49 poor fit (mostly a stack he doesn't have, wrong level), or a location he can't work from.

Red flags are only for real blockers: location/work-authorization limits, required languages or
tools he lacks, wrong seniority, spoken-language requirements. Not industry.
Long postings are shortened by the tracker: you get the start plus every line about location,
visas, authorization, experience and requirements. Never flag a posting as "truncated" or
"excerpt"; judge only what is there. If location or work authorization is genuinely not
mentioned, that is not a red flag for a UAE or "Worldwide" role.

## Jobs
{jobs}

"gig": true only for AI-training work rather than a regular engineering job: contract or per-task
work writing coding tasks, reviewing or rating AI output, or otherwise producing data to train or
evaluate AI models. A normal job at an AI company is NOT a gig. Score gigs on fit as usual.

Reply with ONLY a JSON array, one object per job, same ids:
[{{"id": 123, "score": 0-100, "reasons": ["max 3 short reasons, concrete"], "red_flags": ["e.g. US-only remote, needs Arabic, 3-5 yrs"], "gig": false}}]
"""


# Sentences worth keeping even when they sit at the end of a long posting.
_KEY = re.compile(
    r"\b(locat|based in|remote|hybrid|on-?site|office|relocat|visa|sponsor|authori[sz]|work permit|"
    r"right to work|citizen|resid|time ?zone|utc|gmt|years|yrs|experience|required|must have|"
    r"requirement|qualification|salary|compensation|contract|full-?time|arabic|english|german|french)",
    re.I)
_BOILER = re.compile(r"\b(equal opportunit|eeo|reasonable accommodation|privacy (policy|notice)|"
                     r"cookies?|by applying|disabilit|veteran)", re.I)


def excerpt(text: str, chars: int, extra: int = 1500) -> str:
    """Start of the posting, plus the key sentences (location, visa, authorization,
    experience, requirements) from the rest of it, so nothing that decides fit is lost."""
    text = (text or "").strip()
    if len(text) <= chars + extra:
        return text
    head, rest = text[:chars], text[chars:]
    picked, used = [], 0
    for sent in re.split(r"(?<=[.!?])\s+|\n+", rest):
        sent = sent.strip()
        if len(sent) < 15 or not _KEY.search(sent) or _BOILER.search(sent):
            continue
        if used + len(sent) > extra:
            break
        picked.append(sent)
        used += len(sent) + 1
    tail = "\n".join(picked)
    return head + ("\n[... key lines from the rest of the posting ...]\n" + tail if tail else "")


def _job_block(row, chars: int) -> str:
    desc = excerpt(row["description"] or "", chars)
    return (f"### id={row['id']}\nTitle: {row['title']}\nCompany: {row['company']}\n"
            f"Location: {row['location']}{' (remote)' if row['remote'] else ''}\n"
            f"Salary: {row['salary'] or 'n/a'}\n{desc}\n")


def load_resume(cfg) -> str:
    p: Path = cfg.path("base_resume")
    if not p.exists():
        raise FileNotFoundError(f"base resume not found at {p} (set paths.base_resume in config.yaml)")
    return p.read_text(encoding="utf-8")


def clear_scores(conn) -> int:
    """Forget scores for jobs still waiting (Active/opened) so the next scoring pass redoes them."""
    cur = conn.execute("""UPDATE jobs SET score=NULL, score_reasons=NULL, red_flags=NULL, scored_at=NULL
                          WHERE status IN ('active','opened')""")
    conn.commit()
    return cur.rowcount


def score_pending(conn, cfg, llm: Callable[[str], str] | None = None, job_ids: list[int] | None = None) -> int:
    """Score jobs that have no score yet, or exactly `job_ids` (e.g. a job just added by link)."""
    sc = cfg.section("scorer", default={})
    if job_ids:
        marks = ",".join("?" * len(job_ids))
        rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", list(job_ids)).fetchall()
    else:
        rows = db.jobs_needing_score(conn, sc.get("max_jobs_per_run", 300))
    if not rows:
        return 0
    resume = load_resume(cfg)
    feedback = db.skip_feedback(conn)
    fb_text = ""
    if feedback:
        fb_text = ("- Jobs he recently skipped, and why (score similar jobs lower):\n"
                   + "\n".join(f"  - {f}" for f in feedback) + "\n")
    real = llm is None

    from . import timeouts
    timeout = int(timeouts.score_timeout(conn, cfg))   # a few times a usual scoring call

    def _call(p):
        try:
            return claude_cli.run(p, model=sc.get("model", "haiku"), timeout=timeout)
        finally:
            db.log_claude_usage(conn, "score", claude_cli.last_usage)

    llm = llm or _call
    size = sc.get("batch_size", 12)
    chars = sc.get("description_chars", 3500)
    scored = 0
    for i in range(0, len(rows), size):
        batch = rows[i:i + size]
        prompt = PROMPT.format(resume=resume, feedback=fb_text,
                               jobs="\n".join(_job_block(r, chars) for r in batch))
        try:
            result = claude_cli.extract_json(llm(prompt))
        except claude_cli.UsageLimitError as e:
            log.warning("Claude usage limit reached; leaving %d jobs unscored until next run (%s)",
                        len(rows) - i, e)
            break
        except claude_cli.ClaudeError as e:
            if "timed out" in str(e):          # circuit breaker: don't wait on Claude batch after batch
                log.warning("scoring batch timed out after %ss; leaving %d jobs for the next run",
                            timeout, len(rows) - i)
                break
            log.warning("scoring batch %d failed: %s", i // size, e)
            continue
        except Exception as e:
            log.warning("scoring batch %d failed: %s", i // size, e)
            continue
        valid_ids = {r["id"] for r in batch}
        for item in result if isinstance(result, list) else []:
            try:
                jid = int(item["id"])
                score = max(0, min(100, int(item["score"])))
            except (KeyError, TypeError, ValueError):
                continue
            if jid not in valid_ids:
                continue
            db.set_score(conn, jid, score, list(item.get("reasons", []))[:3], list(item.get("red_flags", []))[:4],
                         gig=item.get("gig") is True)
            scored += 1
        conn.commit()
        log.info("scored batch %d: %d/%d", i // size + 1, scored, len(rows))
    return scored


def compare(conn, cfg, model: str = "sonnet", limit: int = 25,
            llm: Callable[[str], str] | None = None) -> list[dict]:
    """Re-score the most recently scored jobs with another model, WITHOUT saving anything,
    so the two models' scores can be compared side by side."""
    sc = cfg.section("scorer", default={})
    rows = conn.execute(
        """SELECT * FROM jobs WHERE score IS NOT NULL AND status != 'filtered'
           ORDER BY scored_at DESC, id DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return []
    resume = load_resume(cfg)
    feedback = db.skip_feedback(conn)
    fb_text = ("- Jobs he recently skipped, and why (score similar jobs lower):\n"
               + "\n".join(f"  - {f}" for f in feedback) + "\n") if feedback else ""

    def _call(p):
        try:
            return claude_cli.run(p, model=model, timeout=600)
        finally:
            db.log_claude_usage(conn, "compare", claude_cli.last_usage)

    llm = llm or _call
    size, chars = sc.get("batch_size", 12), sc.get("description_chars", 3500)
    new: dict[int, dict] = {}
    for i in range(0, len(rows), size):
        batch = rows[i:i + size]
        prompt = PROMPT.format(resume=resume, feedback=fb_text,
                               jobs="\n".join(_job_block(r, chars) for r in batch))
        try:
            result = claude_cli.extract_json(llm(prompt))
        except claude_cli.UsageLimitError:
            log.warning("Claude usage limit reached; comparing only what was scored so far")
            break
        except Exception as e:
            log.warning("compare batch %d failed: %s", i // size, e)
            continue
        for item in result if isinstance(result, list) else []:
            try:
                new[int(item["id"])] = {"score": max(0, min(100, int(item["score"]))),
                                        "reasons": list(item.get("reasons", []))[:3]}
            except (KeyError, TypeError, ValueError):
                continue
    out = []
    for r in rows:
        if r["id"] not in new:
            continue
        try:
            old_reasons = json.loads(r["score_reasons"] or "[]")
        except ValueError:
            old_reasons = []
        out.append({"id": r["id"], "company": r["company"], "title": r["title"], "status": r["status"],
                    "old": r["score"], "new": new[r["id"]]["score"],
                    "diff": new[r["id"]]["score"] - r["score"],
                    "old_reasons": old_reasons, "new_reasons": new[r["id"]]["reasons"]})
    out.sort(key=lambda x: -abs(x["diff"]))
    return out


def compare_report(results: list[dict], threshold: int, old_model: str, new_model: str,
                   big: int = 15) -> str:
    """Plain-text summary of compare(): agreement, and the jobs that would change."""
    if not results:
        return "Nothing to compare (no scored jobs, or every call failed)."
    n = len(results)
    avg = sum(abs(r["diff"]) for r in results) / n
    up = [r for r in results if r["old"] < threshold <= r["new"]]
    down = [r for r in results if r["new"] < threshold <= r["old"]]
    lines = [f"{new_model} vs {old_model} on {n} jobs: average difference {avg:.0f} points; "
             f"{sum(abs(r['diff']) < big for r in results)} of {n} within {big} points.",
             f"Auto-tailor line ({threshold}+): {len(up)} job(s) would newly qualify, "
             f"{len(down)} would drop out.", ""]
    lines.append(f"{old_model:>7} {new_model:>7}  diff  job")
    for r in results:
        mark = "  <- would auto-tailor" if r in up else ("  <- would no longer auto-tailor" if r in down else "")
        lines.append(f"{r['old']:>7} {r['new']:>7}  {r['diff']:+4d}  #{r['id']} {r['company'][:24]} - "
                     f"{r['title'][:45]}{mark}")
    lines.append("")
    lines.append(f"Biggest disagreements (difference of {big}+ points), with each model's reasons:")
    for r in [r for r in results if abs(r["diff"]) >= big][:10]:
        lines.append(f"\n#{r['id']} {r['company']} - {r['title']}  ({old_model} {r['old']} -> {new_model} {r['new']})")
        lines.append(f"  {old_model}: " + "; ".join(r["old_reasons"]))
        lines.append(f"  {new_model}: " + "; ".join(r["new_reasons"]))
    return "\n".join(lines)
