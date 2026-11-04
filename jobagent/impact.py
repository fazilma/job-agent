"""Shared clarification file for the impact pass: /Users/fazil/Resume/Impact_Questions.md

Each tailoring runs the resume-impact-pass skill without asking anything. Instead it
returns the questions it would have asked (real metrics behind a bullet). They are
merged here, one entry per resume bullet, so the same CoinRoutes bullet is asked once
no matter how many jobs raise it.

You answer in the file, under "Answer:". The next tailoring reads every answered entry
and treats it as a fact you confirmed. Write SKIP to say "no reliable number".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAME = "Impact_Questions.md"

HEADER = """# Impact questions

Questions the resume impact pass would have asked you, one entry per resume bullet.
Answer under **Answer:** with facts you can verify (dashboards, tickets, firm memory).
No estimates. Keep qualifiers exact ("over", "~", "+").
Write `SKIP` if there's no reliable number. Leave it blank to answer later.

Every tailoring after you save this file uses your answers as confirmed facts.
Don't change the `Q-` ids; everything else in an entry can be edited.
"""

_ENTRY = re.compile(r"^### (Q-\d+)\s*[·|-]\s*(.+?)\s*$", re.M)


@dataclass
class Entry:
    id: str
    role: str
    bullet: str
    questions: list[str] = field(default_factory=list)
    asked_by: list[str] = field(default_factory=list)
    answer: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.answer.strip()) and self.answer.strip().upper() != "SKIP"

    @property
    def skipped(self) -> bool:
        return self.answer.strip().upper() == "SKIP"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def key(role: str, bullet: str) -> str:
    return f"{_norm(role)}|{_norm(bullet)[:160]}"


# ---------------------------------------------------------------- parse / render

def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    matches = list(_ENTRY.finditer(text or ""))
    for i, m in enumerate(matches):
        body = text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        body = re.split(r"^## ", body, flags=re.M)[0]          # stop at the next section heading
        bullet = ""
        bm = re.search(r'^\*\*Bullet:\*\*\s*"?(.*?)"?\s*$', body, re.M)
        if bm:
            bullet = bm.group(1).strip()
        asked = []
        am = re.search(r"^\*\*Asked by:\*\*\s*(.*)$", body, re.M)
        if am:
            asked = [a.strip() for a in am.group(1).split(";") if a.strip()]
        qpart = body.split("**Answer:**")[0]
        questions = [q.strip()[2:].strip() for q in qpart.splitlines() if q.strip().startswith("- ")]
        answer = ""
        if "**Answer:**" in body:
            answer = body.split("**Answer:**", 1)[1].strip()
            answer = re.sub(r"(?:^|\n)\s*---\s*$", "", answer).strip()
        entries.append(Entry(id=m.group(1), role=m.group(2).strip(), bullet=bullet,
                             questions=questions, asked_by=asked, answer=answer))
    return entries


def _render_entry(e: Entry) -> str:
    lines = [f"### {e.id} · {e.role}", f'**Bullet:** "{e.bullet}"']
    if e.asked_by:
        lines.append(f"**Asked by:** {'; '.join(e.asked_by[-8:])}")
    lines += [f"- {q}" for q in e.questions]
    lines += ["", "**Answer:**", e.answer.strip(), "", "---", ""]
    return "\n".join(lines)


def render(entries: list[Entry]) -> str:
    waiting = [e for e in entries if not e.answer.strip()]
    answered = [e for e in entries if e.answered]
    skipped = [e for e in entries if e.skipped]
    out = [HEADER, f"**{len(waiting)} waiting** · {len(answered)} answered · {len(skipped)} skipped\n"]
    for title, group in (("Waiting for your answer", waiting), ("Answered", answered), ("Skipped", skipped)):
        if group:
            out.append(f"## {title}\n")
            out += [_render_entry(e) for e in group]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- file operations

def path(cfg) -> Path:
    return cfg.path("resume_workspace") / FILE_NAME


def load(cfg) -> list[Entry]:
    p = path(cfg)
    return parse(p.read_text(encoding="utf-8")) if p.exists() else []


def merge(entries: list[Entry], items: list[dict], job_label: str) -> int:
    """Add new questions from one tailoring. Returns how many new bullets were added."""
    by_key = {key(e.role, e.bullet): e for e in entries}
    next_id = max([int(e.id[2:]) for e in entries] or [0]) + 1
    added = 0
    for it in items or []:
        role, bullet = str(it.get("role", "")).strip(), str(it.get("bullet", "")).strip()
        qs = [str(q).strip() for q in it.get("questions", []) if str(q).strip()]
        if not role or not bullet or not qs:
            continue
        e = by_key.get(key(role, bullet))
        if e is None:
            e = Entry(id=f"Q-{next_id:04d}", role=role, bullet=bullet)
            next_id += 1
            entries.append(e)
            by_key[key(role, bullet)] = e
            added += 1
        if e.answer.strip():
            continue                                 # already answered or skipped: don't re-ask
        known = {_norm(q) for q in e.questions}
        e.questions += [q for q in qs if _norm(q) not in known][: max(0, 3 - len(e.questions))]
        if job_label and job_label not in e.asked_by:
            e.asked_by.append(job_label)
    return added


def record(cfg, items: list[dict], job_label: str) -> int:
    """Merge into the shared file. Re-reads right before writing so your edits aren't lost."""
    p = path(cfg)
    entries = parse(p.read_text(encoding="utf-8")) if p.exists() else []
    added = merge(entries, items, job_label)
    if items:
        p.write_text(render(entries), encoding="utf-8")
    return added


# ---------------------------------------------------------------- prompt helpers

def confirmed_facts_block(entries: list[Entry]) -> str:
    done = [e for e in entries if e.answered]
    if not done:
        return "(none yet)"
    return "\n".join(f'- [{e.id}] {e.role} - bullet "{e.bullet}": {e.answer.strip()}' for e in done)


def already_asked_block(entries: list[Entry]) -> str:
    if not entries:
        return "(none yet)"
    return "\n".join(f'- {e.role}: "{e.bullet[:120]}"' for e in entries)


def extract_questions(text: str) -> list[dict]:
    """Pull the ```impact-questions JSON block out of the tailoring reply."""
    m = re.search(r"```impact-questions\s*(.*?)```", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
