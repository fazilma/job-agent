import json

import httpx
import pytest
from fastapi.testclient import TestClient

from jobagent import addlink, db
from jobagent.config import Config, load_config
from jobagent.models import RawJob
from jobagent.normalize import fingerprint, norm_title

LI_GUEST = """<section class="top-card-layout">
  <h2 class="top-card-layout__title">Senior Software Engineer - Java Full Stack</h2>
  <span class="topcard__flavor"><a class="topcard__org-name-link" href="x">Emirates</a></span>
  <span class="topcard__flavor topcard__flavor--bullet">Dubai, Dubai, United Arab Emirates</span>
  <span class="posted-time-ago__text">19 hours ago</span>
</section>
<div class="show-more-less-html__markup"><p>Java, Spring Boot, microservices, Kafka.</p><ul><li>React</li></ul></div>
<ul><li class="description__job-criteria-item"><h3>Employment type</h3><span>Full-time</span></li></ul>"""

GREENHOUSE_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Staff Backend Engineer",
 "hiringOrganization":{"@type":"Organization","name":"Acme"},
 "jobLocation":{"@type":"Place","address":{"addressLocality":"Dubai","addressCountry":"AE"}},
 "description":"&lt;p&gt;Go and Kafka at scale.&lt;/p&gt;"}</script></head><body>x</body></html>"""


@pytest.fixture
def cfg(tmp_path):
    c = Config(json.loads(json.dumps(load_config())))
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(tmp_path), "base_resume": str(tmp_path / "r.md")}
    return c


def client_for(pages: dict):
    def handler(req):
        body = pages.get(str(req.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- stricter title matching

@pytest.mark.parametrize("a, b, same", [
    ("Senior Software Engineer - Java Full Stack", "Senior Software Engineer - .NET", False),
    ("Senior Software Engineer (Java)", "Senior Software Engineer (Python)", False),
    ("Senior Software Engineer - Java Full Stack", "Senior Software Engineer - Java Full Stack - Dubai", True),
    ("Senior Backend Engineer", "Sr. Backend Engineer (Remote)", True),
    ("Backend Engineer", "Backend Engineer (m/f/d) - Remote, EMEA", True),
    ("Staff Engineer", "Staff Engineer - Job ID 12345", True),
    ("Senior Engineer - Payments", "Senior Engineer - Payments | Dubai | Full-time", True),
    ("Software Engineer, Full-Stack", "Software Engineer (Full-Stack)", True),
])
def test_title_matching(a, b, same):
    assert (norm_title(a) == norm_title(b)) is same


def test_stored_jobs_are_rekeyed_once(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    conn = db.connect(p)
    conn.execute("INSERT INTO jobs(fingerprint, company, title, location, first_seen, last_seen) "
                 "VALUES ('old-key','Emirates','Senior Software Engineer - Java Full Stack','Dubai, UAE','x','x')")
    conn.execute("DELETE FROM meta")                      # pretend it was made by the old version
    conn.commit(); conn.close()
    conn = db.connect(p)
    fp = conn.execute("SELECT fingerprint FROM jobs").fetchone()[0]
    assert fp == fingerprint("Emirates", "Senior Software Engineer - Java Full Stack", "Dubai, UAE", False)
    assert conn.execute("SELECT value FROM meta WHERE key='fingerprint_version'").fetchone()[0] == str(db.FINGERPRINT_VERSION)


# ---------------------------------------------------------------- add by link

def test_linkedin_link_variants():
    for url in ("https://www.linkedin.com/jobs/view/4480656222/",
                "https://ae.linkedin.com/jobs/view/senior-software-engineer-java-at-emirates-4480656222?trk=x",
                "https://www.linkedin.com/jobs/search/?currentJobId=4480656222&keywords=java"):
        assert addlink.linkedin_id(url) == "4480656222"
    assert addlink.source_for("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert addlink.source_for("https://example.com/careers/1") == "manual"


def test_fetch_linkedin_and_merge_with_bayt_copy(cfg):
    conn = db.connect(cfg.path("db"))
    bayt = RawJob("bayt", "Senior Software Engineer - Java Full Stack", "Emirates Group", "Dubai, UAE",
                  url="https://bayt/75355178", description="short")
    jid, _ = db.upsert_job(conn, bayt, fingerprint(bayt.company, bayt.title, bayt.location, False), 1)
    db.set_score(conn, jid, 82, [], [])
    conn.commit()

    c = client_for({"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4480656222": LI_GUEST})
    raw = addlink.fetch("https://www.linkedin.com/jobs/view/4480656222/?trk=abc", client=c)
    assert (raw.source, raw.title, raw.company) == ("linkedin", "Senior Software Engineer - Java Full Stack", "Emirates")
    assert raw.url == "https://www.linkedin.com/jobs/view/4480656222/" and "Kafka" in raw.description

    res = addlink.add(conn, cfg, raw)
    assert res["id"] == jid and not res["is_new"] and res["source"] == "linkedin"   # merged, LinkedIn link shown
    j = db.get_job(conn, jid)
    assert j["url"] == j["apply_url"] == "https://www.linkedin.com/jobs/view/4480656222/"
    assert "Kafka" in j["description"]                                    # the fuller description is kept
    assert set(json.loads(j["sources"])) == {"bayt", "linkedin"} and res["scored"]          # kept its score


def test_fetch_generic_page_uses_jobposting(cfg):
    c = client_for({"https://boards.greenhouse.io/acme/jobs/1": GREENHOUSE_PAGE})
    raw = addlink.fetch("https://boards.greenhouse.io/acme/jobs/1", client=c)
    assert (raw.source, raw.title, raw.company) == ("greenhouse", "Staff Backend Engineer", "Acme")
    assert "Dubai" in raw.location and raw.description == "Go and Kafka at scale."


def test_unreadable_page_asks_for_details(cfg):
    with pytest.raises(addlink.FetchError):
        addlink.fetch("https://www.linkedin.com/jobs/view/123456789/", client=client_for({}))
    with pytest.raises(addlink.FetchError, match="job ID"):
        addlink.fetch("https://www.linkedin.com/feed/", client=client_for({}))


def test_added_job_is_kept_even_if_rules_would_drop_it(cfg):
    conn = db.connect(cfg.path("db"))
    raw = RawJob("manual", "Frontend Developer", "Shop", "Berlin", url="https://shop.example/jobs/1")
    res = addlink.add(conn, cfg, raw)
    assert res["is_new"] and res["status"] == "active" and res["rule_note"]


def test_add_endpoint(cfg, monkeypatch):
    from jobagent import web
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    scored = []
    monkeypatch.setattr(web, "_score_in_background", lambda jid: scored.append(jid))
    api = TestClient(web.app)

    def boom(url, client=None):
        raise addlink.FetchError("blocked")
    monkeypatch.setattr(addlink, "fetch", boom)
    r = api.post("/api/jobs/add", json={"url": "https://www.linkedin.com/jobs/view/4480656222/"}).json()
    assert r == {"ok": False, "need_details": True, "error": "blocked"}

    r = api.post("/api/jobs/add", json={"url": "https://www.linkedin.com/jobs/view/x-4480656222?trk=1",
                                        "title": "Senior Software Engineer - Java Full Stack", "company": "Emirates",
                                        "location": "Dubai", "description": "Java"}).json()
    assert r["ok"] and r["is_new"] and r["source"] == "linkedin" and r["scoring"] and scored == [r["id"]]
    conn = db.connect(cfg.path("db"))
    assert db.get_job(conn, r["id"])["url"] == "https://www.linkedin.com/jobs/view/4480656222/"
    assert "+ Add job" in api.get("/").text
