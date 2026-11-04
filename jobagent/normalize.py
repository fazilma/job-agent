"""Turn the same posting seen on several sites into one fingerprint."""
from __future__ import annotations

import hashlib
import re

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|plc|gmbh|bv|fze|fzco|fz llc|fz-llc|dmcc|difc|adgm|co|corp|corporation|"
    r"group|holdings?|technologies|technology|tech|labs?|international|global|middle east|me)\b\.?")
_TITLE_SYNONYMS = [
    (r"\bsr\b\.?", "senior"),
    (r"\bjr\b\.?", "junior"),
    (r"\bswe\b", "software engineer"),
    (r"\bback[\s-]?end\b", "backend"),
    (r"\bdev\b", "developer"),
    (r"\beng\b", "engineer"),
    (r"\bii\b|\b2\b", "ii"),
    (r"\biii\b|\b3\b", "iii"),
]
_UAE = re.compile(r"\b(dubai|abu dhabi|sharjah|ajman|uae|united arab emirates)\b")


def norm_company(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _COMPANY_SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# Title extras that say where or how, not what the job is: "- Dubai", "(Remote)", "| Full-time".
_PLACES = (r"dubai|abu dhabi|sharjah|ajman|uae|u\.a\.e\.?|united arab emirates|gcc|mena|emea|apac|ksa|"
           r"saudi arabia|riyadh|qatar|doha|bahrain|kuwait|oman|india|bangalore|bengaluru|london|uk|europe|"
           r"eu|us|usa|worldwide|global|anywhere|middle east")
_MODES = (r"remote|hybrid|on[- ]?site|onsite|in[- ]office|office|based|only|first|friendly|full[- ]?time|"
          r"part[- ]?time|permanent|contract|temporary|urgent|urgently|hiring|immediate|joiner|start|"
          r"relocation|visa|sponsorship|sponsored|provided|all genders|[mfwdxh]|"   # (m/f/d) splits into letters
          r"\d+[- ]?(?:months?|years?|weeks?)|months?|years?|weeks?|job|id|req|ref|no|#")
_NOISE_WORD = re.compile(rf"^(?:{_PLACES}|{_MODES}|and|or|with|in|to|of|[a-z]*\d[a-z0-9-]*)$")


def _is_noise(part: str) -> bool:
    """True when a title part is only places / work-mode words, e.g. "Dubai, UAE", "Remote - EMEA"."""
    words = [w for w in re.split(r"[\s,/&+]+", part.strip().lower()) if w]
    if not words:
        return True
    text = " ".join(words)
    # multi-word places/modes ("abu dhabi", "all genders") are checked before single words
    for phrase in ("abu dhabi", "united arab emirates", "saudi arabia", "middle east", "all genders",
                   "full time", "part time", "on site", "in office", "immediate joiner"):
        text = text.replace(phrase, phrase.replace(" ", "_"))
    return all(_NOISE_WORD.match(w.replace("_", " ")) for w in text.split())


def norm_title(title: str) -> str:
    """Normalised title for matching the same job across sites. Keeps what the job is
    ("Senior Software Engineer - Java Full Stack") and drops where/how extras
    ("- Dubai", "(Remote)", "| Full-time"), so "- Java" and "- .NET" roles stay separate."""
    s = title.lower()
    s = re.sub(r"[(\[]([^)\]]*)[)\]]", lambda m: " " if _is_noise(m.group(1)) else f" {m.group(1)} ", s)
    parts = re.split(r"\s[-|–—]\s|\s*\|\s*", s)
    s = " ".join([parts[0]] + [p for p in parts[1:] if not _is_noise(p)])
    for pat, rep in _TITLE_SYNONYMS:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_location(location: str, remote: bool) -> str:
    s = (location or "").lower()
    if remote and not _UAE.search(s):
        return "remote"
    m = _UAE.search(s)
    if m:
        city = m.group(1)
        return "uae" if city in ("uae", "united arab emirates") else city
    return re.sub(r"[^a-z ]+", "", s.split(",")[0]).strip() or "unknown"


def fingerprint(company: str, title: str, location: str, remote: bool) -> str:
    key = f"{norm_company(company)}|{norm_title(title)}|{norm_location(location, remote)}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
