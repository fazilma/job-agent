import json

import httpx
import pytest

from jobagent import db
from jobagent.collectors import ats, discover, workable
from jobagent.config import Config, load_config
from jobagent.models import RawJob


@pytest.fixture
def cfg(tmp_path):
    c = Config(json.loads(json.dumps(load_config())))
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(tmp_path), "base_resume": str(tmp_path / "r.md")}
    c["sources"]["workable"] = {"queries": ["backend engineer"], "location": "United Arab Emirates", "day_range": 30}
    c["sources"]["companies"] = [{"name": "Stripe", "token": "stripe"}]
    return c


def wk_job(i, title, company, loc="Dubai, Dubai, United Arab Emirates", workplace="hybrid"):
    return {"id": f"id-{i}", "title": title, "company": {"title": company}, "locations": [loc],
            "workplace": workplace, "url": f"https://jobs.workable.com/view/{i}/x", "created": "2026-10-29T10:37:51.376Z",
            "description": "<p>Build APIs in Java.</p>", "requirementsSection": "<ul><li>Kafka</li></ul>",
            "benefitsSection": "<p>Visa</p>", "employmentType": "Full-time"}


def test_workable_search_pages_and_fields(cfg):
    pages = {None: {"totalSize": 3, "nextPageToken": "t2", "jobs": [wk_job(1, "Backend Engineer", "Deeplight"),
                                                                   wk_job(2, "Staff Backend Engineer", "Flatgigs", "TELECOMMUTE", "remote")]},
             "t2": {"totalSize": 3, "jobs": [wk_job(3, "Senior Backend Developer", "OrbicTrade")]}}
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=pages[req.url.params.get("pageToken")])

    jobs = workable.collect(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert [j.company for j in jobs] == ["Deeplight", "Flatgigs", "OrbicTrade"]
    assert seen[0] == {"query": "backend engineer", "location": "United Arab Emirates", "day_range": "30"}
    d = jobs[0]
    assert d.source == "workable" and d.location == "Dubai, Dubai, United Arab Emirates" and not d.remote
    assert "Java" in d.description and "Kafka" in d.description and d.posted_at.startswith("2026-10-29")
    assert jobs[1].remote and jobs[1].location == "Remote"


def test_tokens_for():
    assert discover.tokens_for("Property Finder") == ["propertyfinder", "property-finder"]
    assert discover.tokens_for("Fuse Energy LLC")[:2] == ["fuseenergy", "fuse-energy"]
    assert "openinnovationai" in discover.tokens_for("Open Innovation AI")


def test_discovery_finds_boards_with_uae_jobs_only(cfg, monkeypatch):
    conn = db.connect(cfg.path("db"))
    def add(company, loc, n=1, src="linkedin"):
        for i in range(n):
            db.upsert_job(conn, RawJob(src, f"Engineer {company} {i}", company, loc, url=f"https://x/{company}/{i}"),
                          f"fp-{company}-{i}", 1)
    add("Fuse Energy", "Dubai, United Arab Emirates", 3)
    add("Edge", "Abu Dhabi, UAE", 2)
    add("Stripe", "Dubai, UAE")                           # already in config.yaml
    add("Hays (recruiter)", "Dubai, UAE", 5)               # agency
    add("Berlin Co", "Berlin, Germany", 4)                 # not a UAE employer
    conn.commit()

    boards = {("greenhouse", "fuseenergy"): [RawJob("greenhouse", "Backend Engineer", "Fuse Energy", "Dubai")],
              ("lever", "edge"): [RawJob("lever", "Engineer", "Edge (another one)", "Austin, TX")]}
    asked = []

    def fake(kind):
        def f(client, token, company):
            asked.append((kind, token))
            if (kind, token) in boards:
                return boards[(kind, token)]
            raise httpx.HTTPStatusError("404", request=None, response=None)
        return f
    monkeypatch.setattr(ats, "FETCHERS", {k: fake(k) for k in ("greenhouse", "lever", "ashby")})

    res = discover.discover(conn, cfg, client=object())
    assert res["checked"] == 2 and res["found"] == ["Fuse Energy (greenhouse)"]
    assert not any(t in ("stripe", "hays-recruiter", "berlinco") for _, t in asked)
    rows = {r["company"]: dict(r) for r in conn.execute("SELECT * FROM career_boards")}
    assert rows["Fuse Energy"]["status"] == "found" and rows["Fuse Energy"]["token"] == "fuseenergy"
    assert rows["Edge"]["status"] == "none"                # its board has no UAE job: not ours
    assert discover.boards(conn) == [{"name": "Fuse Energy", "token": "fuseenergy", "ats": "greenhouse", "discovered": True}]
    # nothing left to check until the misses are a month old
    assert discover.discover(conn, cfg, client=object())["checked"] == 0
    conn.close()

    # the career-page collector now reads the discovered board too
    monkeypatch.setattr(ats, "detect", lambda client, token, name, kinds=None: (kinds[0] if kinds else "greenhouse",
                                                                                   [RawJob("x", "T", name, "Dubai")]))
    got = ats.collect(cfg)
    assert sorted({j.company for j in got}) == ["Fuse Energy", "Stripe"]
