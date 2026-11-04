from __future__ import annotations

from dataclasses import dataclass, field

# Job lifecycle. "Lists" in the dashboard are just filters over these.
ACTIVE = "active"            # waiting for you
OPENED = "opened"            # you clicked Apply; not yet confirmed
APPLIED = "applied"
INTERVIEWING = "interviewing"
SKIPPED = "skipped"
CLOSED = "closed"            # posting gone, rejected, offer done, etc.
FILTERED = "filtered"        # dropped by rules; kept so it's never re-added

STATUSES = [ACTIVE, OPENED, APPLIED, INTERVIEWING, SKIPPED, CLOSED, FILTERED]

# Sources whose apply link goes straight to the company (preferred over boards).
DIRECT_SOURCES = {"greenhouse", "lever", "ashby", "smartrecruiters", "dover", "workable"}


@dataclass
class RawJob:
    """One posting as a collector returns it, before dedupe."""

    source: str                    # linkedin, bayt, greenhouse, ...
    title: str
    company: str
    location: str = ""
    url: str = ""                  # listing page
    apply_url: str = ""            # where to apply (falls back to url)
    description: str = ""          # plain text
    external_id: str = ""
    remote: bool = False
    salary: str = ""
    posted_at: str = ""            # ISO date if known
    extra: dict = field(default_factory=dict)
