from __future__ import annotations

from . import agencies, apify_boards, ats, linkedin_public, remote_boards, remote_boards2, web_search, workable

# name -> (config key under `sources`, function)
COLLECTORS = {
    "linkedin": ("linkedin", apify_boards.collect_linkedin),
    "linkedin_public": ("linkedin_public", linkedin_public.collect),
    "bayt": ("bayt", apify_boards.collect_bayt),
    "naukrigulf": ("naukrigulf", apify_boards.collect_naukrigulf),
    "career_pages": ("companies", ats.collect),
    "workable": ("workable", workable.collect),
    "agencies": ("agencies", agencies.collect),
    "remoteok": ("remoteok", remote_boards.collect_remoteok),
    "weworkremotely": ("weworkremotely", remote_boards.collect_wwr),
    "himalayas": ("himalayas", remote_boards2.collect_himalayas),
    "remotive": ("remotive", remote_boards2.collect_remotive),
    "workingnomads": ("workingnomads", remote_boards2.collect_workingnomads),
    "nodesk": ("nodesk", remote_boards2.collect_nodesk),
    "jobspresso": ("jobspresso", remote_boards2.collect_jobspresso),
    "remoterocketship": ("remoterocketship", remote_boards2.collect_remoterocketship),
    "web_search": ("web_search", web_search.collect),
}


def enabled(cfg) -> dict:
    out = {}
    for name, (key, fn) in COLLECTORS.items():
        section = cfg.section("sources", key)
        if section is None:
            continue
        if isinstance(section, dict) and not section.get("enabled", True):
            continue
        out[name] = fn
    return out
