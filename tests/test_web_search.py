import json
from datetime import date, timedelta

import httpx
import pytest

from jobagent import db
from jobagent.collectors import web_search
from jobagent.config import Config, load_config
from jobagent.models import RawJob


@pytest.fixture
def cfg(tmp_path):
    c = Config(json.loads(json.dumps(load_config())))
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(tmp_path), "base_resume": str(tmp_path / "r.md")}
    c["sources"]["web_search"] = {"queries": ["backend Dubai"], "max_age_days": 30}
    return c


def page(title="Senior Backend Engineer", company="Acme", days_old=2, expires=None):
    ld = {"@type": "JobPosting", "title": title, "hiringOrganization": {"name": company},
          "jobLocation": {"address": {"addressLocality": "Dubai", "addressCountry": "AE"}},
          "description": "<p>Build APIs</p>", "datePosted": (date.today() - timedelta(days=days_old)).isoformat()}
    if expires:
        ld["validThrough"] = expires
    return f'<script type="application/ld+json">{json.dumps(ld)}</script>'


def run(cfg, pages, urls=None):
    fetched = []

    def handler(req):
        fetched.append(str(req.url))
        return httpx.Response(200, text=pages.get(str(req.url), "<html>no posting</html>"))

    items = [{"url": u, "title": "Senior Backend Engineer"} for u in (urls or pages)]
    jobs = web_search.collect(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)),
                              search=lambda q, n: items)
    return jobs, fetched


def test_keeps_only_recent_real_postings(cfg):
    past = (date.today() - timedelta(days=1)).isoformat()
    jobs, _ = run(cfg, {"https://a.example/new": page(),
                        "https://a.example/old": page(days_old=90),
                        "https://a.example/expired": page(expires=past),
                        "https://a.example/listing": "<html><title>Senior Backend Engineer jobs</title></html>"})
    assert [j.url for j in jobs] == ["https://a.example/new"]
    assert (jobs[0].source, jobs[0].company) == ("web_search", "Acme")
    assert "Dubai" in jobs[0].location and jobs[0].posted_at


def test_one_bad_page_does_not_lose_the_run(cfg):
    jobs, _ = run(cfg, {"https://a.example/list-title": page(title=["Senior Backend Engineer"]),
                        "https://a.example/list-company": page(company=["Acme"]),
                        "https://a.example/good": page()})
    assert [j.url for j in jobs] == ["https://a.example/good"]


def test_known_job_is_returned_again_so_it_stays_open(cfg):
    conn = db.connect(cfg.path("db"))
    db.upsert_job(conn, RawJob(source=web_search.SOURCE, title="T", company="C", url="https://a.example/job"), "fp", 1)
    conn.commit()
    conn.close()
    jobs, _ = run(cfg, {"https://a.example/job": page()})
    assert [j.url for j in jobs] == ["https://a.example/job"]


def test_web_text_never_replaces_existing_description(cfg):
    conn = db.connect(cfg.path("db"))
    job_id, _ = db.upsert_job(conn, RawJob(source="greenhouse", title="T", company="C", description="real"), "fp", 1)
    db.upsert_job(conn, RawJob(source=web_search.SOURCE, title="T", company="C", description="planted, and longer"), "fp", 1)
    assert db.get_job(conn, job_id)["description"] == "real"
    db.upsert_job(conn, RawJob(source="workable", title="T", company="C", description="real, and longer"), "fp", 1)
    assert db.get_job(conn, job_id)["description"] == "real, and longer"    # other sources still merge
    conn.close()


def test_unsafe_and_irrelevant_results_are_not_fetched(cfg):
    urls = ["http://127.0.0.1:8765/", "http://localhost/x", "http://[::1]/x",
            "file:///etc/passwd", "https://ae.linkedin.com/posts/x", ""]

    def handler(req):
        raise AssertionError(f"fetched {req.url}")

    items = [{"url": u, "title": "Senior Backend Engineer"} for u in urls]
    items.append({"url": "https://a.example/mgr", "title": "Engineering Manager"})
    assert web_search.collect(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)),
                              search=lambda q, n: items) == []


def test_failed_search_reaches_circuit_breaker_but_keeps_partial_results(cfg):
    def down(q, n):
        raise RuntimeError("agent-reach RATE_LIMITED")

    with pytest.raises(RuntimeError, match="RATE_LIMITED"):
        web_search.collect(cfg, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
                           search=down)

    cfg["sources"]["web_search"]["queries"] = ["one", "two", "three"]
    calls = []

    def partial(q, n):
        calls.append(q)
        if q == "two":
            raise RuntimeError("agent-reach RATE_LIMITED")
        return [{"url": "https://a.example/new", "title": "Senior Backend Engineer"}]

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page())))
    assert len(web_search.collect(cfg, client=client, search=partial)) == 1
    assert calls == ["one", "two"]      # stops pushing after a rate limit


def test_missing_cli_skips_quietly(cfg, monkeypatch):
    monkeypatch.setattr(web_search, "_bin", lambda: None)
    assert web_search.collect(cfg) == []


def test_web_jobs_never_tailor_automatically(cfg):
    """Unattended tailoring has file and network tools, so open-web text must not reach it."""
    conn = db.connect(cfg.path("db"))
    for fp, src in (("a", "web_search"), ("b", "greenhouse")):
        job_id, _ = db.upsert_job(conn, RawJob(source=src, title=src, company=fp), fp, 1)
        conn.execute("UPDATE jobs SET score=95 WHERE id=?", (job_id,))
    # found on the web first, then on a board: the description may still be the web page's text
    db.upsert_job(conn, RawJob(source="web_search", title="merged", company="c", description="x" * 99), "c", 1)
    db.upsert_job(conn, RawJob(source="workable", title="merged", company="c"), "c", 1)
    conn.execute("UPDATE jobs SET score=95 WHERE fingerprint='c'")
    assert [r["fingerprint"] for r in db.jobs_to_tailor(conn, 80, 10)] == ["b"]

    conn.execute("UPDATE jobs SET tailor_status='queued' WHERE fingerprint='a'")    # clicking Tailor still works
    assert "a" in [r["fingerprint"] for r in db.jobs_to_tailor(conn, 80, 10)]
    conn.close()
