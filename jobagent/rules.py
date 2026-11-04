"""Cheap keyword filter that runs before any AI cost is spent."""
from __future__ import annotations

import re
from functools import lru_cache

from .models import RawJob


@lru_cache(maxsize=1024)
def _term(term: str) -> re.Pattern:
    # word-ish boundaries that also work for terms like ".net" or "back-end"
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def _has_any(text: str, terms) -> str | None:
    t = (text or "").lower()
    for term in terms or []:
        if _term(term).search(t):
            return term
    return None


@lru_cache(maxsize=1024)
def _word(word: str) -> re.Pattern:
    # a whole word, plural allowed: "engineer" matches "Engineers"
    return re.compile(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"s?(?![a-z0-9])")


def _has_all_words(text: str, lines) -> str | None:
    """The first line whose words all appear in text, in any order, with anything between:
    "engineer software" matches "Senior Engineer - Software Development"."""
    t = (text or "").lower()
    for line in lines or []:
        words = str(line).split()
        if words and all(_word(w).search(t) for w in words):
            return line
    return None


def _strip_terms(text: str, terms) -> str:
    t = (text or "").lower()
    for term in terms or []:
        t = _term(term).sub(" ", t)
    return re.sub(r"[^a-z]+", " ", t).strip()


def title_check(job: RawJob, rules: dict) -> str | None:
    """The company/title part of the rules: why the job is dropped, or None if it passes."""
    title = job.title or ""
    hit = _has_any(job.company, rules.get("company_exclude"))
    if hit:
        return f"company excluded ({hit})"
    if not (_has_any(title, rules.get("title_include")) or _has_all_words(title, rules.get("title_include_words"))):
        return "title not in target roles"
    hit = _has_any(title, rules.get("title_exclude"))
    if hit:
        return f"title excluded ({hit})"
    return None


def check(job: RawJob, rules: dict) -> tuple[bool, str]:
    """Returns (keep, reason). Reason explains drops, or the location bucket when kept."""
    why = title_check(job, rules)
    if why:
        return False, why

    loc = (job.location or "").strip()
    desc = job.description or ""
    if _has_any(loc, rules.get("uae_terms")):
        return True, "uae"

    remote_terms = rules.get("remote_terms")
    loc_says_remote = bool(_has_any(loc, remote_terms))
    if loc_says_remote or job.remote:
        ok_region = _has_any(loc, rules.get("remote_ok_regions"))
        # "Remote" on its own, with "only"/"first" noise removed, means no region restriction.
        leftover = _strip_terms(loc, list(remote_terms or []) + ["only", "first", "friendly", "fully", "100"])
        if ok_region or (loc_says_remote and not leftover) or not loc:
            blocked = _has_any(f"{loc}\n{desc[:6000]}", rules.get("remote_blocklist"))
            if blocked:
                return False, f"remote but restricted ({blocked})"
            return True, "remote"
        if loc_says_remote:
            # "Remote - USA", "Remote (Canada)": remote, but only from that country.
            return False, f"remote only within {loc}"
        # Flagged remote by the source but the location is a specific city/country
        # (e.g. Ashby's "US - San Francisco" hybrid roles): treat as on-site abroad.

    if _has_any(desc, rules.get("relocation_terms")):
        return True, "abroad with relocation/visa"
    return False, f"outside UAE ({loc or 'no location'}), no visa/relocation mention"


# ---------------------------------------------------------------- AI-training gigs
# Contract work that trains or evaluates AI models (writing coding tasks, reviewing AI output),
# e.g. G2i "Senior Software Engineer, AI Training", Xperteez, micro1, Askable. Kept, but tagged,
# so they don't sit next to real engineering roles or get a resume automatically.
_GIG_TITLE = re.compile(r"\bAI[ -]?(training|trainers?|tutors?|coding tasks?)\b|\bRLHF\b|"
                        r"\b(LLM|model)s? (training|evaluat\w*|trainers?)\b|\bdata annotat", re.I)
_GIG_AI = re.compile(r"\bhelp(s|ing)? train\b|\bmeasure and train\b|\bRLHF\b|\bAI training (project|tasks?|data quality)\b|"
                     r"\bevaluat\w*\s+(the quality of\s+)?(interactions with\s+)?(modern\s+)?"
                     r"(AI[- ]generated|AI coding agents|coding agents)|"
                     r"\bnot a traditional engineering role\b|\b(shape|improve) how (AI )?models (learn|reason)", re.I)
_GIG_TERMS = re.compile(r"\bcontract(or)?\b|\bper task\b|\bper hour\b|/\s?h(ou)?r\b|\bhrs?\s*(/|per|a)\s*week\b|"
                        r"\bhours? (a|per) week\b|\bfreelance\b", re.I)


def gig_reason(title: str, description: str) -> str | None:
    """Why this looks like an AI-training gig, or None. The description test needs both
    AI-training wording and contract/hourly wording, so a full-time job that builds
    AI-training infrastructure isn't caught."""
    m = _GIG_TITLE.search(title or "")
    if m:
        return f"title: {m.group(0)}"
    d = description or ""
    a = _GIG_AI.search(d)
    if a and _GIG_TERMS.search(d):
        return f"description: {a.group(0)}"
    return None
