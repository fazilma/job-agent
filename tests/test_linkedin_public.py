import json

import httpx
import pytest

from jobagent import db
from jobagent.collectors import linkedin_public as lp
from jobagent.config import Config, load_config


def card(jid, title, company, location="Dubai, United Arab Emirates", date="2026-10-26"):
    return f"""<li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{jid}">
      <a class="base-card__full-link" href="https://ae.linkedin.com/jobs/view/x-{jid}?position=1"></a>
      <div class="base-search-card__info"><h3 class="base-search-card__title">  {title} </h3>
      <h4 class="base-search-card__subtitle"><a href="#">{company}</a></h4>
      <div class="base-search-card__metadata"><span class="job-search-card__location">{location}</span>
      <time class="job-search-card__listdate" datetime="{date}">1 day ago</time></div></div></div></li>"""


DETAIL = """<h2 class="top-card-layout__title">Senior Backend Engineer</h2>
<a class="topcard__org-name-link">Kraken</a><span class="topcard__flavor--bullet">United Arab Emirates</span>
<div class="show-more-less-html__markup"><p>Rust, Go and Kafka. Distributed systems.</p></div>"""


@pytest.fixture
def cfg(tmp_path):
    c = Config(json.loads(json.dumps(load_config())))
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(tmp_path), "base_resume": str(tmp_path / "r.md")}
    c["sources"]["linkedin_public"] = {
        "max_per_search": 30, "delay_seconds": 0, "details_per_run": 5,
        "urls": ["https://www.linkedin.com/jobs/search-results/?currentJobId=4481595123&showHowYouFit=HOW_YOU_FIT"
                 "&keywords=Senior%20Backend%20Engineer&origin=QUALIFICATION_LANDING&geoId=104305776"],
        "searches": []}
    return c


def test_params_from_pasted_link():
    p = lp.params_from_url("https://www.linkedin.com/jobs/search-results/?currentJobId=4481595123&showHowYouFit="
                           "HOW_YOU_FIT&keywords=Senior%20Backend%20Engineer&origin=QUALIFICATION_LANDING"
                           "&originToLandingJobPostings=4481595123%2C4478676345&geoId=104305776&f_WT=2")
    assert p == {"keywords": "Senior Backend Engineer", "geoId": "104305776", "f_WT": "2"}


def test_collect_pages_details_and_skips(cfg):
    conn = db.connect(cfg.path("db"))                    # one job already in the tracker
    conn.execute("INSERT INTO jobs(fingerprint, company, title, url, first_seen, last_seen) VALUES "
                 "('k','Bybit','Principal Backend Engineer','https://ae.linkedin.com/jobs/view/x-2222222222','x','x')")
    conn.commit(); conn.close()
    page0 = "".join([card(1111111111, "Senior Backend Engineer", "Kraken", "United Arab Emirates"),
                     card(2222222222, "Principal Backend Engineer", "Bybit"),
                     card(3333333333, "Frontend Developer", "Shop")] + [card(4000000000 + i, f"Data Analyst {i}", "X") for i in range(7)])
    page1 = card(1111111111, "Senior Backend Engineer", "Kraken") + card(5555555555, "Senior Java Developer", "Dautom")
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if "seeMoreJobPostings" in req.url.path:
            assert req.url.params["keywords"] == "Senior Backend Engineer" and req.url.params["geoId"] == "104305776"
            assert req.url.params["f_TPR"] == "r604800" and "currentJobId" not in req.url.params
            return httpx.Response(200, text={"0": page0, "10": page1}.get(req.url.params["start"], ""))
        return httpx.Response(200, text=DETAIL)

    jobs = lp.collect(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    by_id = {j.external_id: j for j in jobs}
    assert len(by_id) == 11 and by_id["1111111111"].source == "linkedin"
    assert by_id["1111111111"].url == "https://www.linkedin.com/jobs/view/1111111111/"
    assert by_id["1111111111"].posted_at == "2026-10-26" and "Kafka" in by_id["1111111111"].description
    details = [u for u in seen if "jobPosting/" in u]
    assert sorted(u.split("/")[-1] for u in details) == ["1111111111", "5555555555"]   # new + title passes only
    assert by_id["2222222222"].description == "" and by_id["3333333333"].description == ""


def test_rate_limit_pauses_once_then_stops_with_what_it_has(cfg):
    calls, sleeps = [], []

    def handler(req):
        calls.append(req.url.params.get("start"))
        if req.url.params.get("start") == "0":
            return httpx.Response(200, text="".join(card(6000000000 + i, "Senior Backend Engineer", f"C{i}") for i in range(10)))
        return httpx.Response(429)

    jobs = lp.collect(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    assert len(jobs) == 10 and 60 in sleeps                 # paused once, then kept page 1's jobs
    assert all(j.description == "" for j in jobs)           # stopped before reading descriptions
