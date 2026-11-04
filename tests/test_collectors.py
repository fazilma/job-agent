import httpx

from jobagent.collectors import ats, remote_boards
from jobagent.collectors.apify_boards import map_item
from jobagent.config import Config


def test_apify_mapping_variants():
    li = map_item("linkedin", {"title": "Senior Backend Engineer", "companyName": "Deriv", "location": "Dubai",
                                "jobUrl": "https://li/1", "descriptionText": "Java", "postedAt": "2026-10-13T10:00:00Z"})
    assert (li.company, li.posted_at, li.url) == ("Deriv", "2026-10-13", "https://li/1")
    bayt = map_item("bayt", {"title": "Java Dev", "company": {"name": "Bank"}, "location": ["Abu Dhabi", "UAE"],
                             "url": "https://bayt/1", "description": "<p>Spring</p>", "isRemote": False})
    assert bayt.company == "Bank" and bayt.location == "Abu Dhabi, UAE" and bayt.description == "Spring"
    assert map_item("x", {"title": "No company"}) is None


def _mock(routes):
    def handler(req):
        for frag, payload in routes.items():
            if frag in str(req.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ats_autodetect():
    client = _mock({
        "api.lever.co/v0/postings/acme": [{"id": "a1", "text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/a1",
                                           "applyUrl": "https://jobs.lever.co/acme/a1/apply", "categories": {"location": "Dubai"},
                                           "descriptionPlain": "Kafka", "lists": [{"text": "You", "content": "<li>Java</li>"}],
                                           "createdAt": 1760000000000, "workplaceType": "hybrid"}],
        "boards-api.greenhouse.io/v1/boards/gh": {"jobs": [{"id": 7, "title": "SRE", "absolute_url": "https://gh/7",
                                                             "location": {"name": "Remote"}, "content": "&lt;p&gt;Go&lt;/p&gt;"}]},
    })
    kind, jobs = ats.detect(client, "acme", "Acme")
    assert kind == "lever" and jobs[0].apply_url.endswith("/apply") and "Java" in jobs[0].description
    kind, jobs = ats.detect(client, "gh", "GH")
    assert kind == "greenhouse" and jobs[0].description == "Go" and jobs[0].remote
    assert ats.detect(client, "nope", "Nope") == (None, [])


def test_remoteok_tag_filter(monkeypatch):
    payload = [{"legal": "notice"},
               {"id": 1, "position": "Backend Engineer", "company": "A", "tags": ["backend", "java"], "url": "u1",
                "description": "<b>x</b>", "epoch": 1760000000, "salary_min": 120000, "salary_max": 160000},
               {"id": 2, "position": "Designer", "company": "B", "tags": ["design"], "url": "u2"}]
    monkeypatch.setattr(remote_boards, "http_client", lambda: _mock({"remoteok.com/api": payload}))
    jobs = remote_boards.collect_remoteok(Config({"sources": {"remoteok": {"tags": ["backend"]}}}))
    assert [j.title for j in jobs] == ["Backend Engineer"] and jobs[0].salary == "USD 120,000-160,000"



def _fake_apify(monkeypatch, call_sig, run_obj):
    from jobagent.collectors import apify_boards
    calls = {}

    class Actor:
        pass
    def call(self, **kw):
        calls.update(kw)
        return run_obj
    call.__signature__ = call_sig
    Actor.call = call

    class Dataset:
        def __init__(self, i): calls["dataset"] = i
        def iterate_items(self): return iter([{"title": "Backend Engineer", "companyName": "X"}])

    class Client:
        def actor(self, name): return Actor()
        def dataset(self, i): return Dataset(i)
    monkeypatch.setattr(apify_boards, "_client", lambda: Client())
    return apify_boards, calls


def test_run_actor_apify_client_v3(monkeypatch):
    import inspect
    from datetime import timedelta
    from types import SimpleNamespace
    sig = inspect.Signature([inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)] + [
        inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None)
        for n in ("run_input", "run_timeout", "wait_duration", "logger", "timeout")])
    mod, calls = _fake_apify(monkeypatch, sig, SimpleNamespace(default_dataset_id="ds1", status="SUCCEEDED"))
    items = mod.run_actor("a/b", {"q": 1}, timeout_secs=100)
    assert calls["run_timeout"] == timedelta(seconds=100) and "timeout_secs" not in calls
    assert "logger" in calls and calls["logger"] is None                # no live-log thread
    assert calls["dataset"] == "ds1" and items[0]["title"] == "Backend Engineer"


def test_run_actor_apify_client_v1(monkeypatch):
    import inspect
    sig = inspect.Signature([inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)] + [
        inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None)
        for n in ("run_input", "timeout_secs", "wait_secs")])
    mod, calls = _fake_apify(monkeypatch, sig, {"defaultDatasetId": "ds2", "status": "SUCCEEDED"})
    mod.run_actor("a/b", {}, timeout_secs=100)
    assert calls["timeout_secs"] == 100 and calls["dataset"] == "ds2"


def test_linkedin_skips_known_jobs_and_searches_past_week(tmp_path, monkeypatch):
    import json as _json
    from jobagent import db
    from jobagent.collectors import apify_boards
    from jobagent.config import load_config
    from jobagent.models import RawJob
    cfg = Config(_json.loads(_json.dumps(load_config())))
    cfg["paths"]["db"] = str(tmp_path / "jobs.db")
    conn = db.connect(cfg.path("db"))
    db.upsert_job(conn, RawJob("linkedin", "Java Backend Developer", "InfoVision", "Dubai",
                               url="https://ae.linkedin.com/jobs/view/java-backend-developer-at-infovision-4477577612"), "fp", 1)
    conn.commit()
    inputs = []
    monkeypatch.setattr(apify_boards, "account_usage", lambda: None)          # fallback cap
    monkeypatch.setattr(apify_boards, "run_actor", lambda actor, ri, timeout_secs=240, max_charge_usd=None: inputs.append(ri) or [])
    apify_boards.collect_linkedin(cfg)
    assert inputs and all(i["skipJobId"] == ["4477577612"] for i in inputs)
    assert all(i["datePosted"] == "r604800" and i["limit"] == 35 for i in inputs)   # $0.015 fallback
    assert {"java backend developer", "backend developer"} <= {i["keywords"] for i in inputs}



def _budget_cfg():
    import json as _json
    from jobagent.config import load_config
    return Config(_json.loads(_json.dumps(load_config())))


def _usage(used, days_left):
    from datetime import datetime, timedelta, timezone
    end = (datetime.now(timezone.utc) + timedelta(days=days_left)).isoformat()
    return {"used": used, "limit": 5.0, "cycle_end": end}


def test_budget_paces_spend_to_the_reset_date():
    from jobagent.collectors import apify_boards as ab
    cfg = _budget_cfg()
    plan = ab.budget_plan(cfg, _usage(0.5, 23))
    assert abs(plan["today"] - (5.0 - 0.5 - 0.25) / 23) < 0.002            # ~0.185 USD per run
    n_li = len(cfg["sources"]["linkedin"]["searches"])
    assert abs(plan["per_search"]["linkedin"] * n_li - plan["today"] * 0.8) < 1e-6
    li = ab.results_for("linkedin", plan["per_search"]["linkedin"], 150)
    assert 25 <= li <= 40                                                    # ~34 new jobs per search
    # spending the whole plan every day for 23 days stays under the $5
    assert plan["today"] * 23 + 0.5 <= 5.0


def test_budget_used_up_skips_apify(monkeypatch):
    from jobagent.collectors import apify_boards as ab
    cfg = _budget_cfg()
    monkeypatch.setattr(ab, "account_usage", lambda: _usage(4.9, 10))
    called = []
    monkeypatch.setattr(ab, "run_actor", lambda *a, **k: called.append(1) or [])
    assert ab.collect_linkedin(cfg) == [] and ab.collect_bayt(cfg) == [] and ab.collect_naukrigulf(cfg) == []
    assert called == []                                                      # nothing spent


def test_each_search_gets_a_hard_dollar_cap(monkeypatch):
    import inspect
    from decimal import Decimal
    from types import SimpleNamespace
    from jobagent.collectors import apify_boards as ab
    sig = inspect.Signature([inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)] + [
        inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None)
        for n in ("run_input", "run_timeout", "wait_duration", "logger", "max_total_charge_usd")])
    mod, calls = _fake_apify(monkeypatch, sig, SimpleNamespace(default_dataset_id="d", status="SUCCEEDED"))
    mod.run_actor("a/b", {}, max_charge_usd=0.0148)
    assert calls["max_total_charge_usd"] == Decimal("0.0148")


def test_bayt_naukri_incremental_with_stable_memory(monkeypatch):
    from jobagent.collectors import apify_boards as ab
    cfg = _budget_cfg()
    seen = []
    monkeypatch.setattr(ab, "account_usage", lambda: _usage(0.5, 23))
    monkeypatch.setattr(ab, "run_actor", lambda actor, ri, **k: seen.append(ri) or [])
    ab.collect_bayt(cfg)
    ab.collect_naukrigulf(cfg)
    assert all(r["incrementalMode"] for r in seen)
    assert {r["stateKey"] for r in seen} >= {"jobagent-bayt-java-developer-ae"}
    assert any(r.get("datePosted") == "past-week" for r in seen) and any(r.get("datePosted") == 7 for r in seen)


def test_dover_careers_page():
    base = "https://app.dover.com/api/v1"
    jobs = {"count": 3, "next": None, "results": [
        {"id": "j1", "title": "Senior Backend Engineer", "workplace_type": "REMOTE", "is_published": True, "is_sample": False,
         "locations": [{"location_type": "REMOTE", "name": "Worldwide", "location_option": {"display_name": "Worldwide"}}]},
        {"id": "j2", "title": "Software Engineer", "workplace_type": "REMOTE", "is_published": True,
         "locations": [{"location_type": "REMOTE", "name": "Canada"}, {"location_type": "REMOTE", "name": "United States"}]},
        {"id": "j3", "title": "Head of People", "workplace_type": "REMOTE", "is_published": True,
         "locations": [{"location_type": "REMOTE", "name": "United States"}]}]}
    detail = {"user_provided_description": "<p>Python, Postgres.</p>", "visa_support": True,
              "compensation": {"currency_code": "USD", "lower_bound": 150000, "upper_bound": 190000, "salary_range_type": "YEARLY"},
              "created": "2026-10-10T00:00:00Z"}
    fetched = []

    def handler(req):
        u = str(req.url)
        if u == f"{base}/careers-page-slug/acme":
            return httpx.Response(200, json={"id": "cid", "slug": "acme", "name": "Acme AI"})
        if u.startswith(f"{base}/careers-page/cid/jobs"):
            return httpx.Response(200, json=jobs)
        if u.startswith(f"{base}/inbound/application-portal-job/"):
            fetched.append(u.rsplit("/", 1)[-1])
            return httpx.Response(200, json=detail)
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = ats._dover(client, "acme", "Acme", title_ok=lambda t: "engineer" in t.lower(), known=set())
    assert [j.location for j in out] == ["Remote (Worldwide)", "Remote (Canada) / Remote (United States)",
                                         "Remote (United States)"]
    assert fetched == ["j1", "j2"]                                    # no detail call for Head of People
    j1 = out[0]
    assert j1.url == "https://app.dover.com/apply/Acme%20AI/j1/" and j1.remote
    assert "Python" in j1.description and "Visa sponsorship available." in j1.description
    assert j1.salary == "USD 150,000-190,000 yearly" and j1.posted_at == "2026-10-10"
    from jobagent import rules
    from jobagent.config import load_config
    flt = load_config()["filter"]
    assert [rules.check(j, flt)[0] for j in out[:2]] == [True, False]  # US/Canada-only remote dropped
