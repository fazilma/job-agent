import json

import httpx
import pytest

from jobagent import rules
from jobagent.collectors import remote_boards2 as rb
from jobagent.config import Config, load_config


@pytest.fixture
def cfg():
    return Config(json.loads(json.dumps(load_config())))


def mock(monkeypatch, routes, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        for frag, resp in routes.items():
            if frag in str(req.url):
                return resp(req) if callable(resp) else resp
        return httpx.Response(404)
    monkeypatch.setattr(rb, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def keep(cfg, job):
    return rules.check(job, cfg["filter"])[0]


def test_himalayas_uae_filter_and_country_restrictions(cfg, monkeypatch):
    seen = []
    page1 = {"jobs": [
        {"guid": "https://himalayas.app/companies/acme/jobs/1", "title": "Senior Backend Engineer", "companyName": "Acme",
         "locationRestrictions": [], "description": "<p>Go</p>", "pubDate": 1760400000, "applicationLink": "https://acme/apply",
         "minSalary": 120000, "maxSalary": 150000, "currency": "USD"},
        {"guid": "https://himalayas.app/companies/b/jobs/2", "title": "Software Engineer", "companyName": "B",
         "locationRestrictions": [{"name": "United Arab Emirates"}, {"name": "Saudi Arabia"}], "description": "x"}]}
    mock(monkeypatch, {"himalayas.app/jobs/api/search": httpx.Response(200, json=page1)}, seen)
    cfg["sources"]["himalayas"]["queries"] = ["software engineer"]
    jobs = rb.collect_himalayas(cfg)
    assert seen[0].url.params["country"] == "AE"
    assert [j.location for j in jobs] == ["Remote (Worldwide)", "Remote (United Arab Emirates, Saudi Arabia)"]
    assert jobs[0].salary == "USD 120,000-150,000 annual" and jobs[0].apply_url == "https://acme/apply"
    assert all(keep(cfg, j) for j in jobs)


def test_himalayas_stops_on_rate_limit(cfg, monkeypatch):
    mock(monkeypatch, {"himalayas.app": httpx.Response(429)})
    assert rb.collect_himalayas(cfg) == []


def test_remotive_country_restrictions_respected(cfg, monkeypatch):
    data = {"jobs": [
        {"id": 1, "title": "Senior Backend Engineer", "company_name": "A", "candidate_required_location": "Worldwide",
         "url": "https://remotive.com/remote-jobs/software-dev/a-1", "description": "<p>Java</p>", "publication_date": "2026-10-13T10:00:00"},
        {"id": 2, "title": "Senior Backend Engineer", "company_name": "B", "candidate_required_location": "USA Only",
         "url": "https://remotive.com/remote-jobs/software-dev/b-2", "description": "x"},
        {"id": 3, "title": "Software Engineer", "company_name": "C", "candidate_required_location": "EMEA",
         "url": "https://remotive.com/remote-jobs/software-dev/c-3", "description": "x"}]}
    mock(monkeypatch, {"remotive.com/api/remote-jobs": httpx.Response(200, json=data)})
    jobs = rb.collect_remotive(cfg)
    assert [keep(cfg, j) for j in jobs] == [True, False, True]
    assert jobs[0].posted_at == "2026-10-13"


def test_working_nomads_category_and_location(cfg, monkeypatch):
    data = [
        {"url": "https://www.workingnomads.com/job/go/1/", "title": "Senior Backend Developer", "company_name": "A",
         "category_name": "Development", "location": "Global", "description": "<p>x</p>", "pub_date": "2026-10-14T00:32:03-04:00"},
        {"url": "https://www.workingnomads.com/job/go/2/", "title": "Software Engineer", "company_name": "B",
         "category_name": "Development", "location": "USA", "description": "x"},
        {"url": "https://www.workingnomads.com/job/go/3/", "title": "Language specialist", "company_name": "C",
         "category_name": "Administration", "location": "Global", "description": "x"}]
    mock(monkeypatch, {"workingnomads.com/api/exposed_jobs": httpx.Response(200, json=data)})
    jobs = rb.collect_workingnomads(cfg)
    assert [j.company for j in jobs] == ["A", "B"]                     # admin job dropped by category
    assert [keep(cfg, j) for j in jobs] == [True, False]               # "Remote (USA)" can't hire you


NODESK = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Remote Jobs on NoDesk</title>
<item><title>Senior Backend Engineer at Acme</title><link>https://nodesk.co/remote-jobs/acme-sbe/</link>
<pubDate>Mon, 12 Oct 2026 08:00:00 +0000</pubDate><description>Python and Postgres.</description></item>
<item><title>Staff Software Engineer (USA Only, 100% Remote) at Close</title><link>https://nodesk.co/remote-jobs/close-sse/</link>
<pubDate>Mon, 12 Oct 2026 08:00:00 +0000</pubDate><description>x</description></item></channel></rss>"""


def test_nodesk_title_parsing_and_usa_only(cfg, monkeypatch):
    mock(monkeypatch, {"nodesk.co/remote-jobs/index.xml": httpx.Response(200, content=NODESK)})
    jobs = rb.collect_nodesk(cfg)
    assert [(j.title, j.company) for j in jobs] == [("Senior Backend Engineer", "Acme"), ("Staff Software Engineer", "Close")]
    assert jobs[1].location == "Remote (USA Only, 100% Remote)"
    assert [keep(cfg, j) for j in jobs] == [True, False]
    assert jobs[0].posted_at == "2026-10-12"


JOBSPRESSO = b"""<?xml version="1.0"?><rss version="2.0" xmlns:job_listing="https://wpjobmanager.com"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item><title>Senior Full Stack Developer</title><link>https://jobspresso.co/job/acme-fsd/</link>
<job_listing:company>Acme</job_listing:company><job_listing:location>Anywhere</job_listing:location>
<content:encoded><![CDATA[<p>React and Node.</p>]]></content:encoded><pubDate>Tue, 13 Oct 2026 09:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_jobspresso_wp_job_manager_feed(cfg, monkeypatch):
    seen = []
    mock(monkeypatch, {"jobspresso.co": httpx.Response(200, content=JOBSPRESSO)}, seen)
    jobs = rb.collect_jobspresso(cfg)
    assert seen[0].url.params["feed"] == "job_feed"
    j = jobs[0]
    assert (j.company, j.location, j.description) == ("Acme", "Remote (Anywhere)", "React and Node.")
    assert keep(cfg, j)


def test_remoterocketship_needs_key_and_skips_sandbox(cfg, monkeypatch):
    monkeypatch.delenv("REMOTEROCKETSHIP_API_KEY", raising=False)
    assert rb.collect_remoterocketship(cfg) == []                       # no key: nothing, no error

    monkeypatch.setenv("REMOTEROCKETSHIP_API_KEY", "k")
    bodies = []
    def handler(req):
        bodies.append(json.loads(req.content))
        assert req.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json={"jobOpenings": [
            {"id": 9, "roleTitle": "Senior Backend Engineer", "url": "https://rr/9", "company": {"name": "Acme"},
             "roleDescription": "Kafka", "roleRequirements": "Java"}],
            "pagination": {"hasNextPage": False}})
    real = httpx.Client
    monkeypatch.setattr(rb.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    jobs = rb.collect_remoterocketship(cfg)
    assert bodies[0]["filters"]["locationFilters"] == ["United Arab Emirates"] and bodies[0]["includeJobDescription"]
    assert jobs[0].company == "Acme" and "Kafka" in jobs[0].description and "Java" in jobs[0].description
    assert keep(cfg, jobs[0])


def test_old_boards_off_new_ones_on(cfg):
    from jobagent import collectors
    on = set(collectors.enabled(cfg))
    assert {"himalayas", "remotive", "workingnomads", "nodesk", "jobspresso", "remoterocketship"} <= on
    assert not ({"remoteok", "weworkremotely"} & on)
