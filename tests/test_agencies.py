"""Agency scrapers against small fixture pages shaped like each live site."""
import json

import httpx
import pytest

from jobagent import db
from jobagent.collectors import agencies, ats
from jobagent.config import Config, load_config
from jobagent.models import RawJob
from jobagent.normalize import fingerprint


def ld(title, city="Dubai", country="AE", desc="<p>Java, Spring Boot, Kafka. 8+ years.</p>", org="Hays"):
    return ('<script type="application/ld+json">' + json.dumps({
        "@context": "https://schema.org", "@type": "JobPosting", "title": title, "description": desc,
        "datePosted": "2026-10-10", "hiringOrganization": {"@type": "Organization", "name": org},
        "jobLocation": {"@type": "Place", "address": {"addressLocality": city, "addressCountry": country}},
    }) + "</script>")


SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>"""

PAGES = {
    # Hays: sitemap + JSON-LD detail
    "https://www.hays.ae/sitemap/en-AE/job-sitemap.xml": SITEMAP.format(
        "<url><loc>https://www.hays.ae/job-detail/senior-java-developer_4567</loc></url>"
        "<url><loc>https://www.hays.ae/job-detail/finance-manager_4568</loc></url>"),
    "https://www.hays.ae/job-detail/senior-java-developer_4567": "<html>" + ld("Senior Java Developer") + "</html>",
    # Umatr
    "https://www.umatr.io/sitemap.xml": SITEMAP.format(
        "<url><loc>https://www.umatr.io/jobs/99-senior-backend-engineer</loc></url>"
        "<url><loc>https://www.umatr.io/about</loc></url>"),
    "https://www.umatr.io/jobs/99-senior-backend-engineer": "<html>" + ld("Senior Backend Engineer", org="Acme Tech") + "</html>",
    # Marc Ellis: WordPress REST (no location in list -> detail page)
    "https://www.marc-ellis.com/wp-json/wp/v2/job-listings": [
        {"id": 11, "title": {"rendered": "Full Stack Developer"}, "link": "https://www.marc-ellis.com/job/fsd-11",
         "excerpt": {"rendered": "<p>React and Node.</p>"}, "date": "2026-10-09T10:00:00"}],
    "https://www.marc-ellis.com/job/fsd-11": "<html>" + ld("Full Stack Developer", city="Abu Dhabi", org="Marc Ellis") + "</html>",
    # Discovered: JSON API
    "https://www.discovered.ae/api/v1/jobs": {"page_count": 1, "jobs": [
        {"id": 5, "job_title": "Senior Software Engineer", "cached_slug": "sse-5", "job_location": "Dubai",
         "clean_description": "Python, Django, AWS. " * 20}]},
    # Halian: one server-rendered page, location in CSS classes
    "https://www.halian.com/jobs": """<ul>
        <li class="js-job-item js-city--dubai js-country--united-arab-emirates">
          <a href="/jobs/senior-backend-developer-123"><p>Senior Backend Developer</p></a></li></ul>""",
    "https://www.halian.com/jobs/senior-backend-developer-123": "<html><main><h1>Senior Backend Developer</h1><p>"
        + "Build Java microservices for a bank. " * 30 + "</p></main></html>",
    # Charterhouse: paged listing
    "https://www.charterhouseme.ae/jobs/information-technology": """<ul><li class="job-result-item">
        <div class="job-title"><a href="/job/software-engineer-77">Software Engineer</a></div>
        <div class="results-job-location">Dubai</div><div class="job-description">Go and Kafka.</div></li></ul>""",
    # Michael Page
    "https://www.michaelpage.ae/jobs/technology": """<div class="job-tile"><h3>
        <a href="/job-detail/lead-developer/ref/jn-102026-6612345">Lead Developer</a></h3>
        <div class="job-location">Dubai</div></div>""",
    # Salt
    "https://welovesalt.com/jobs": """<ul><li class="job-item"><div class="job-item__title">
        <a href="https://welovesalt.com/jobs/senior-python-developer-98765">Senior Python Developer</a></div>
        <div class="highlights"><i class="highlights__item-icon--location"></i><span>Dubai</span></div></li></ul>""",
    # Jex Digital (Wix): link text is "Know More", title in a nearby heading
    "https://www.jexdigital.co/jobs": """<div><div><h3>Implementation Engineer</h3>
        <p>Location Dubai, UAE Level Senior</p><a href="https://www.jexdigital.co/jobs/implementation-engineer">Know More</a></div></div>""",
}


def transport(pages=PAGES, hits=None):
    def handler(req: httpx.Request):
        url = str(req.url).split("?")[0]
        if hits is not None:
            hits.append(str(req.url))
        page = req.url.params.get("page")
        if page not in (None, "0", "1"):                     # every fixture is a single page
            body = [] if "wp-json" in url else ("{}" if "api/v1" in url else "<html></html>")
            return httpx.Response(200, json=body) if isinstance(body, list) else httpx.Response(200, text=body)
        if url not in pages:
            return httpx.Response(404, text="not found")
        body = pages[url]
        if isinstance(body, (list, dict)):
            return httpx.Response(200, json=body, headers={"X-WP-TotalPages": "1"})
        return httpx.Response(200, text=body)
    return httpx.MockTransport(handler)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = Config(json.loads(json.dumps(load_config())))
    c["paths"]["db"] = str(tmp_path / "jobs.db")
    c["sources"]["agencies"]["delay_seconds"] = [0, 0]
    c["sources"]["agencies"]["salt_keywords"] = ["developer"]
    monkeypatch.setattr(agencies, "http_client", lambda: httpx.Client(transport=transport(), follow_redirects=True))
    return c


def by_source(jobs):
    out = {}
    for j in jobs:
        out.setdefault(j.source, []).append(j)
    return out


def test_every_agency_parses(cfg):
    jobs = by_source(agencies.collect(cfg, None))
    assert set(jobs) == set(agencies.SITES)

    hays = jobs["hays"]
    assert [j.title for j in hays] == ["Senior Java Developer"]          # finance manager skipped by title
    assert hays[0].location == "Dubai, AE" and "Kafka" in hays[0].description
    assert hays[0].company == "Hays (recruiter)"                          # Hays names itself, keep agency

    assert jobs["umatr"][0].company == "Acme Tech"                        # real client named in JSON-LD
    assert jobs["marcellis"][0].location == "Abu Dhabi, AE"               # filled from detail page
    assert jobs["discovered"][0].location == "Dubai" and len(jobs["discovered"][0].description) > 200
    h = jobs["halian"][0]
    assert h.location == "Dubai, United Arab Emirates" and "microservices" in h.description
    assert jobs["charterhouse"][0].url == "https://www.charterhouseme.ae/job/software-engineer-77"
    assert jobs["michaelpage"][0].external_id.lower() == "jn-102026-6612345"
    assert jobs["salt"][0].location == "Dubai" and jobs["salt"][0].external_id == "98765"
    assert jobs["michaelpage"][0].location == "Dubai"
    jex = jobs["jexdigital"][0]
    assert jex.title == "Implementation Engineer" and jex.location == "Dubai, UAE"


def test_known_urls_are_not_refetched(cfg, monkeypatch):
    conn = db.connect(cfg.path("db"))
    url = "https://www.hays.ae/job-detail/senior-java-developer_4567"
    raw = RawJob("hays", "Senior Java Developer", "Hays (recruiter)", "Dubai", url=url)
    db.upsert_job(conn, raw, fingerprint(raw.company, raw.title, raw.location, False), run_id=1)
    conn.commit()
    hits = []
    monkeypatch.setattr(agencies, "http_client",
                        lambda: httpx.Client(transport=transport(hits=hits), follow_redirects=True))
    agencies.collect(cfg, None, sites=["hays"])
    assert url not in hits                                               # sitemap read, detail skipped


def test_listing_vs_detail_title_is_one_job(cfg):
    """Day 1 stores the detail-page title/location; day 2 only sees the listing. Same URL -> same row."""
    conn = db.connect(cfg.path("db"))
    url = "https://www.hays.ae/job-detail/sr-java-dev_1"
    a = RawJob("hays", "Senior Java Developer (Fintech)", "Hays (recruiter)", "Dubai, AE", url=url)
    b = RawJob("hays", "Sr Java Dev", "Hays (recruiter)", "", url=url)
    id1, new1 = db.upsert_job(conn, a, fingerprint(a.company, a.title, a.location, False), 1)
    id2, new2 = db.upsert_job(conn, b, fingerprint(b.company, b.title, b.location, False), 2)
    assert new1 and not new2 and id1 == id2


def test_one_broken_site_doesnt_stop_others(cfg, monkeypatch):
    pages = dict(PAGES)
    pages.pop("https://www.halian.com/jobs")                             # Halian 404s
    monkeypatch.setattr(agencies, "http_client", lambda: httpx.Client(transport=transport(pages)))
    jobs = by_source(agencies.collect(cfg, None))
    assert "halian" not in jobs and "hays" in jobs and "salt" in jobs


def test_smartrecruiters_talabat():
    api = "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings"
    def handler(req):
        u = str(req.url)
        if u.startswith(api + "?"):
            assert req.url.params["country"] == "ae"
            return httpx.Response(200, json={"totalFound": 2, "content": [
                {"id": "744", "name": "Senior Backend Engineer", "releasedDate": "2026-10-11T08:00:00Z",
                 "location": {"city": "Dubai", "country": "ae", "remote": False}, "ref": f"{api}/744"},
                {"id": "745", "name": "Marketing Lead", "location": {"city": "Dubai", "country": "ae"}, "ref": f"{api}/745"}]})
        if u == f"{api}/744":
            return httpx.Response(200, json={"applyUrl": "https://jobs.smartrecruiters.com/DeliveryHero/744/apply",
                "jobAd": {"sections": {"jobDescription": {"title": "Role", "text": "<p>Go, Kafka</p>"},
                                       "qualifications": {"title": "You", "text": "<li>Java</li>"}}}})
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = ats._smartrecruiters(client, "DeliveryHero", "Delivery Hero (talabat)", countries=["ae"],
                                title_ok=lambda t: "engineer" in t.lower(), known=set())
    assert [j.title for j in jobs] == ["Senior Backend Engineer", "Marketing Lead"]
    assert "Kafka" in jobs[0].description and "Java" in jobs[0].description
    assert jobs[0].apply_url.endswith("/744/apply") and jobs[0].location == "Dubai, AE"
    assert jobs[1].description == ""                                     # irrelevant title: no detail fetch


def test_blank_location_on_uae_only_site_means_uae(cfg, monkeypatch):
    pages = dict(PAGES)
    pages["https://www.michaelpage.ae/jobs/technology"] = pages["https://www.michaelpage.ae/jobs/technology"].replace(
        '<div class="job-location">Dubai</div>', "")
    monkeypatch.setattr(agencies, "http_client", lambda: httpx.Client(transport=transport(pages)))
    mp = agencies.collect(cfg, None, sites=["michaelpage"])[0]
    assert mp.location == "UAE"
    from jobagent import rules
    assert rules.check(mp, cfg["filter"]) == (True, "uae")


def test_pipeline_end_to_end_with_agencies(cfg):
    from jobagent import pipeline
    stats = pipeline.run(cfg, sources=["agencies"], skip_score=True, skip_tailor=True,
                         collector_overrides={"agencies": agencies.collect})
    assert stats["sources"]["agencies"]["jobs"] == 9
    conn = db.connect(cfg.path("db"))
    rows = conn.execute("SELECT title, company, status FROM jobs ORDER BY title").fetchall()
    assert all(r["status"] == "active" for r in rows), [dict(r) for r in rows]
    # second run: same jobs, nothing new, no duplicates
    stats2 = pipeline.run(cfg, sources=["agencies"], skip_score=True, skip_tailor=True,
                          collector_overrides={"agencies": agencies.collect})
    assert stats2["new"] == 0 and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 9
