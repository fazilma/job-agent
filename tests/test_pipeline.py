import json
import re

import pytest
from fastapi.testclient import TestClient

from jobagent import db, pipeline, rules
from jobagent.config import Config, load_config
from jobagent.models import RawJob
from jobagent.normalize import fingerprint

DESC = "Build low latency order routing in Java and Python. 10+ years. Visa sponsorship available."


def jobs_day1():
    return [
        RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai, United Arab Emirates", url="https://li/1", description=DESC),
        # same job on the company's own ATS -> merged, direct link preferred
        RawJob("greenhouse", "Sr. Backend Engineer", "Deriv FZ-LLC", "Dubai", url="https://deriv.gh/1", apply_url="https://deriv.gh/1/apply", description=DESC + " More detail."),
        RawJob("remoteok", "Staff Software Engineer, Payments", "Acme Pay", "Worldwide", url="https://rok/2", remote=True, description=DESC),
        RawJob("remoteok", "Senior Backend Engineer", "USCo", "Remote", url="https://rok/3", remote=True, description="US only. Java."),
        RawJob("bayt", "Frontend Developer", "Shop", "Dubai", url="https://bayt/4"),
        RawJob("linkedin", "Senior Software Engineer, Trading", "HFT Ltd", "Singapore", url="https://li/5", description=DESC),
        RawJob("linkedin", "Backend Engineer", "LocalCo", "Berlin", url="https://li/6", description="Hybrid, Berlin office"),
    ]


def jobs_day2():
    return [
        RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai, UAE", url="https://li/1", description=DESC),
        RawJob("bayt", "Senior Java Developer", "Bank X", "Abu Dhabi", url="https://bayt/7", description=DESC),
    ]


@pytest.fixture
def cfg(tmp_path):
    c = load_config()
    c = Config(json.loads(json.dumps(c)))
    ws = tmp_path / "resumes"
    (ws / "Backend_2026").mkdir(parents=True)
    (ws / "Backend_2026" / "resume.md").write_text("Fazil. Java, Python, trading systems, 10 years.")
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(ws), "base_resume": str(ws / "Backend_2026" / "resume.md")}
    return c


def fake_scorer(prompt):
    ids = [int(x) for x in re.findall(r"### id=(\d+)", prompt)]
    return "```json\n" + json.dumps([{"id": i, "score": 90 - 15 * n, "reasons": ["good match"], "red_flags": []}
                                     for n, i in enumerate(ids)]) + "\n```"


def make_fake_tailor(workspace):
    def fake(prompt, cwd):
        company = re.search(r"Company: (.+)", prompt).group(1).strip()
        out = workspace / company.replace(" ", "_")
        out.mkdir(exist_ok=True)
        (out / "Fazil_Resume.pdf").write_bytes(b"%PDF-1.4 fake")
        return f"Done.\n```application-answers\nWhy {company}: ...\n```\nOUTPUT_DIR={out}"
    return fake


def run(cfg, jobs):
    from pathlib import Path
    ws = Path(cfg["paths"]["resume_workspace"])
    return pipeline.run(cfg, collector_overrides={"fake": lambda c, r: jobs},
                        scorer_llm=fake_scorer, tailor_llm=make_fake_tailor(ws))


def test_fingerprint_merges_board_and_ats():
    a = fingerprint("Deriv", "Senior Backend Engineer", "Dubai, United Arab Emirates", False)
    b = fingerprint("Deriv FZ-LLC", "Sr. Backend Engineer", "Dubai", False)
    c = fingerprint("Deriv", "Senior Backend Engineer", "Remote", True)
    assert a == b and a != c


def test_rules(cfg):
    r = cfg["filter"]
    assert rules.check(RawJob("x", "Senior Backend Engineer", "A", "Dubai"), r) == (True, "uae")
    assert rules.check(RawJob("x", "Frontend Developer", "A", "Dubai"), r)[0] is False
    assert rules.check(RawJob("x", "Senior .NET Backend Engineer", "A", "Dubai"), r)[0] is False
    assert rules.check(RawJob("x", "Backend Engineer", "A", "Remote", remote=True, description="US only"), r)[0] is False
    assert rules.check(RawJob("x", "Backend Engineer", "A", "Berlin", description="relocation package"), r)[0] is True
    assert rules.check(RawJob("x", "Backend Engineer", "A", "Berlin"), r)[0] is False


def test_daily_runs_append_and_keep_status(cfg):
    s1 = run(cfg, jobs_day1())
    assert s1["new"] == 3            # Deriv (merged), Acme Pay, HFT Singapore
    assert s1["new_filtered"] == 3   # US-only remote, frontend, Berlin w/o visa
    assert s1["merged"] == 1
    conn = db.connect(cfg.path("db"))
    deriv = conn.execute("SELECT * FROM jobs WHERE company='Deriv'").fetchone()
    assert deriv["apply_url"] == "https://deriv.gh/1/apply"          # direct link won
    assert set(json.loads(deriv["sources"])) == {"linkedin", "greenhouse"}
    assert s1["scored"] == 3 and s1["tailored"] == 1                 # only 90 >= 80; 75 and 60 are not
    assert deriv["tailor_status"] == "done" and "Why Deriv: ..." in deriv["answers"]

    db.set_status(conn, deriv["id"], "applied")
    s2 = run(cfg, jobs_day2())
    assert s2["new"] == 1 and s2["merged"] == 1
    deriv = db.get_job(conn, deriv["id"])
    assert deriv["status"] == "applied"                              # re-seen, status kept
    total = conn.execute("SELECT COUNT(*) FROM jobs WHERE status!='filtered'").fetchone()[0]
    assert total == 4                                                # appended, not replaced
    counts = db.tab_counts(conn)
    assert counts["new"] == 1 and counts["applied"] == 1 and counts["active"] == 3


def test_auto_close(cfg):
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    conn.execute("UPDATE jobs SET last_seen='2020-01-01T00:00:00+00:00' WHERE company='HFT Ltd'")
    conn.commit()
    assert db.auto_close(conn, 14) == 1
    assert conn.execute("SELECT status FROM jobs WHERE company='HFT Ltd'").fetchone()[0] == "closed"


def test_dashboard_api(cfg, monkeypatch):
    run(cfg, jobs_day1())
    from jobagent import web
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    monkeypatch.setattr(web, "_reveal", lambda p: None)
    client = TestClient(web.app)
    assert "Job Tracker" in client.get("/").text
    data = client.get("/api/jobs", params={"tab": "active"}).json()
    assert len(data["jobs"]) == 3 and data["jobs"][0]["score"] == 90
    jid = data["jobs"][0]["id"]

    client.post(f"/api/jobs/{jid}/apply")
    data = client.get("/api/jobs", params={"tab": "active"}).json()
    assert [p["id"] for p in data["pending"]] == [jid]                # "did you submit?" prompt

    client.post(f"/api/jobs/{jid}/status", json={"status": "applied"})
    assert client.get("/api/jobs", params={"tab": "applied"}).json()["counts"]["applied"] == 1

    other = data["jobs"][1]["id"]
    client.post(f"/api/jobs/{other}/status", json={"status": "skipped", "reason": "Wrong stack"})
    sk = client.get("/api/jobs", params={"tab": "skipped"}).json()["jobs"]
    assert sk[0]["skip_reason"] == "Wrong stack"
    detail = client.get(f"/api/jobs/{other}").json()
    assert [h["to_status"] for h in detail["history"]] == ["active", "skipped"]
    assert client.get(f"/files/resume/{jid}").status_code == 200
    assert client.post(f"/api/jobs/{jid}/status", json={"status": "bogus"}).status_code == 400


def test_skip_feedback_reaches_scorer(cfg):
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    jid = conn.execute("SELECT id FROM jobs WHERE company='HFT Ltd'").fetchone()[0]
    db.set_status(conn, jid, "skipped", "needs C++")
    seen = {}
    def spy(prompt):
        seen["p"] = prompt
        return fake_scorer(prompt)
    from jobagent import scorer
    conn.execute("UPDATE jobs SET score=NULL WHERE status='active'")
    conn.commit()
    scorer.score_pending(conn, cfg, llm=spy)
    assert "needs C++" in seen["p"]


def test_tailor_prompt_uses_skill_unattended(cfg):
    from jobagent import tailor
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    row = conn.execute("SELECT * FROM jobs WHERE company='Deriv'").fetchone()
    p = tailor.build_prompt(cfg, row)
    assert "anthropic-skills:tailor-resume" in p
    assert "Tailored resume only" in p and "Do not ask" in p
    assert "`deriv_senior-backend-engineer`" in p
    assert "```application-answers" in p and "Current location" not in p   # fixed fields: Python
    assert "Current location:** Dubai, UAE" in tailor.answers_file(cfg, row, "x")
    cfg["tailor"]["extras"] = ["gap_analysis"]
    assert "Gap analysis (Gap_Analysis.md)" in tailor.build_prompt(cfg, row)


def test_all_tab_and_manual_tailor(cfg, monkeypatch):
    run(cfg, jobs_day1())
    from jobagent import web, tailor
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    queued = []
    monkeypatch.setattr(tailor, "enqueue", lambda jid, f: queued.append(jid))
    client = TestClient(web.app)
    all_jobs = client.get("/api/jobs", params={"tab": "all"}).json()
    assert len(all_jobs["jobs"]) == 3 and all_jobs["counts"]["filtered"] == 3
    with_filtered = client.get("/api/jobs", params={"tab": "all", "include_filtered": "true"}).json()["jobs"]
    assert len(with_filtered) == 6
    dropped = next(j for j in with_filtered if j["status"] == "filtered")
    assert dropped["filter_reason"]
    # a filtered job can still be tailored by hand
    assert client.post(f"/api/jobs/{dropped['id']}/tailor").json()["ok"]
    assert queued == [dropped["id"]]
    assert client.get(f"/api/jobs/{dropped['id']}").json()["tailor_status"] == "queued"
    # second click while queued doesn't queue twice
    assert client.post(f"/api/jobs/{dropped['id']}/tailor").json().get("already")
    page = client.get(f"/jobs/{dropped['id']}/jd").text
    assert f"const ID = {dropped['id']};" in page


def test_auto_tailor_above_threshold_uses_skill(cfg):
    from pathlib import Path
    ws = Path(cfg["paths"]["resume_workspace"])
    fake = make_fake_tailor(ws)
    prompts = []
    def spy(prompt, cwd):
        prompts.append(prompt)
        return fake(prompt, cwd)
    stats = pipeline.run(cfg, collector_overrides={"fake": lambda c, r: jobs_day1()},
                         scorer_llm=fake_scorer, tailor_llm=spy)
    assert stats["tailored"] == 1 and len(prompts) == 1             # threshold 80: only the 90
    assert all(p.startswith("Use the anthropic-skills:tailor-resume skill") for p in prompts)


@pytest.mark.parametrize("title,loc,remote,desc,keep", [
    ("Senior Backend Engineer, Agentic AI", "US - San Francisco", True, "Hybrid, 3 days in office", False),  # Airwallex
    ("Senior Software Engineer", "Remote - USA", True, "", False),
    ("Software Engineer", "Remote - Canada", True, "", False),
    ("Senior Software Engineer", "Remote", True, "", True),
    ("Senior Software Engineer", "Remote - EMEA", True, "", True),
    ("Backend Engineer", "Remote (Anywhere in the World)", True, "", True),
    ("Backend Engineer", "Remote (USA Only)", True, "", False),
    ("Backend Engineer", "Remote", True, "Applicants must be US-based", False),
    ("Senior Backend Engineer", "Dubai, United Arab Emirates", False, "", True),
    ("Forward Deployed Engineer", "Abu Dhabi", False, "", True),
    ("Implementation Engineer", "Dubai", False, "", True),
    ("Senior Full-Stack Engineer", "Remote - Worldwide", True, "", True),
    ("Java Developer", "Dubai", False, "", True),
    ("Senior Infrastructure Engineer, Trading", "Dubai", False, "", False),   # not in your title list
    ("Payments Risk Analyst II", "Remote", True, "", False),
    ("Engineering Manager, Backend", "Dubai", False, "", False),
    ("Senior Software Engineer", "London", False, "Relocation package and visa sponsorship", True),
])
def test_rules_targeting(cfg, title, loc, remote, desc, keep):
    assert rules.check(RawJob("x", title, "Co", loc, remote=remote, description=desc), cfg["filter"])[0] is keep


def test_refilter_cleans_active_but_not_applied(cfg):
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    conn.execute("UPDATE jobs SET location='Remote - USA', remote=1 WHERE company IN ('Acme Pay', 'Deriv')")
    db.set_status(conn, conn.execute("SELECT id FROM jobs WHERE company='Deriv'").fetchone()[0], "applied")
    from jobagent.pipeline import refilter
    out = refilter(conn, cfg)
    assert out["dropped"] == 1 and out["revived"] == 0
    st = dict(conn.execute("SELECT company, status FROM jobs WHERE company IN ('Acme Pay','Deriv')").fetchall())
    assert st == {"Acme Pay": "filtered", "Deriv": "applied"}


def test_scorer_is_domain_neutral_and_rescore(cfg):
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    from jobagent import scorer
    assert scorer.clear_scores(conn) == 3
    seen = {}
    def spy(p):
        seen["p"] = p
        return fake_scorer(p)
    assert scorer.score_pending(conn, cfg, llm=spy) == 3
    assert "Business domain does NOT matter" in seen["p"]
    assert "trading & execution infrastructure" not in seen["p"]


def test_usage_limit_stops_tailoring_and_requeues(cfg):
    from pathlib import Path
    from jobagent import tailor, claude_cli
    run(cfg, jobs_day1())                       # tailors 2 with the fake
    conn = db.connect(cfg.path("db"))
    conn.execute("UPDATE jobs SET tailor_status='none', resume_pdf=NULL WHERE status='active'")
    conn.execute("UPDATE jobs SET score=95 WHERE status='active'")
    # one job failed last night with a raw CLI blob (cut off before the limit text)
    conn.execute("""UPDATE jobs SET tailor_status='failed', tailor_error='{"duration_api_ms":0,"stop_reason":...'
                    WHERE company='HFT Ltd'""")
    conn.commit()
    calls = []
    def limited(prompt, cwd):
        calls.append(1)
        raise claude_cli.UsageLimitError("You've hit your session limit")
    assert tailor.tailor_top(conn, cfg, llm=limited) == 0
    assert len(calls) == 1                                           # stopped after the first limit
    st = dict(conn.execute("SELECT company, tailor_status FROM jobs WHERE status='active'").fetchall())
    assert set(st.values()) == {"none"}                              # nothing left as failed/running


def test_usage_limit_stops_scoring(cfg):
    from jobagent import scorer, claude_cli
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    scorer.clear_scores(conn)
    cfg["scorer"]["batch_size"] = 1
    calls = []
    def limited(p):
        calls.append(1)
        raise claude_cli.UsageLimitError("limit")
    assert scorer.score_pending(conn, cfg, llm=limited) == 0 and len(calls) == 1


def test_cli_usage_limit_detection(monkeypatch):
    import json, subprocess
    from jobagent import claude_cli
    blob = json.dumps({"is_error": True, "api_error": "usage_limit_reached",
                       "result": "You've hit your session limit · resets 11:30pm"})
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=blob, stderr=""))
    with pytest.raises(claude_cli.UsageLimitError, match="session limit"):
        claude_cli.run("hi")
    other = json.dumps({"is_error": True, "result": "Overloaded"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=other, stderr=""))
    with pytest.raises(claude_cli.ClaudeError, match="^Overloaded$"):
        claude_cli.run("hi")


def test_tailor_prompt_is_lean(cfg):
    from jobagent import tailor
    run(cfg, jobs_day1())
    row = db.connect(cfg.path("db")).execute("SELECT * FROM jobs WHERE company='Deriv'").fetchone()
    assert "do NOT spawn subagents" in tailor.build_prompt(cfg, row)
    assert "Agent" not in tailor.DEFAULT_TOOLS and "Task" not in tailor.DEFAULT_TOOLS


def test_resume_ready_filter(cfg, monkeypatch):
    run(cfg, jobs_day1())                                            # threshold 80: Deriv tailored
    from jobagent import web
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    client = TestClient(web.app)
    for tab in ("all", "active"):
        data = client.get("/api/jobs", params={"tab": tab, "resume_ready": "true"}).json()
        assert [j["company"] for j in data["jobs"]] == ["Deriv"]
        assert all(j["has_pdf"] for j in data["jobs"])
    assert data["counts"]["resume_ready"] == 1
    assert len(client.get("/api/jobs", params={"tab": "all"}).json()["jobs"]) == 3   # off = everything
    # a failed tailoring never counts as ready
    conn = db.connect(cfg.path("db"))
    conn.execute("UPDATE jobs SET tailor_status='failed' WHERE company='Deriv'")
    conn.commit()
    assert client.get("/api/jobs", params={"tab": "all", "resume_ready": "true"}).json()["jobs"] == []


def test_scorer_excerpt_keeps_late_location_lines():
    from jobagent.scorer import excerpt, PROMPT
    long = "We build great products for customers. " * 300 + \
        "You must be based in the UAE with a valid work permit. 7+ years of Java required. " \
        "We are an equal opportunity employer."
    e = excerpt(long, 3000)
    assert len(e) < 4600 and "based in the UAE" in e and "7+ years" in e and "equal opportunity" not in e
    assert 'Never flag a posting as "truncated"' in PROMPT
    short = "Short JD. Remote."
    assert excerpt(short, 3000) == short


def test_enrich_fills_short_descriptions(cfg):
    import httpx
    from jobagent import enrich
    run(cfg, jobs_day1())
    conn = db.connect(cfg.path("db"))
    conn.execute("UPDATE jobs SET score=NULL, description='tiny', url='https://jobs.example.com/acme/1' WHERE company='Acme Pay'")
    conn.execute("UPDATE jobs SET score=NULL, description='tiny', url='https://www.linkedin.com/jobs/view/1' WHERE company='HFT Ltd'")
    conn.commit()
    page = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"x","description":"<p>'
            + "Build payment rails in Go and Kafka. " * 40 + '</p>"}</script></html>')
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, text=page)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg["enrich"]["delay_seconds"] = 0
    assert enrich.fill_short_descriptions(conn, cfg, client=client) == 1
    assert calls == ["https://jobs.example.com/acme/1"]                 # LinkedIn skipped
    d = conn.execute("SELECT description FROM jobs WHERE company='Acme Pay'").fetchone()[0]
    assert "payment rails" in d and len(d) > 800


def test_system_page(cfg, monkeypatch, tmp_path):
    run(cfg, jobs_day1())
    from datetime import time as dtime
    from jobagent import daily, web
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setitem(daily.STATE, "enabled", True)
    monkeypatch.setitem(daily.STATE, "at", dtime(7, 30))
    client = TestClient(web.app)
    assert "System" in client.get("/system").text and 'href="/system"' in client.get("/").text

    d = client.get("/api/system").json()
    assert d["schedule"]["kind"] == "docker" and d["schedule"]["at"] == "07:30" and d["schedule"]["next_run"]
    assert len(d["runs"]) == 1 and d["runs"][0]["stats"]["new"] == 3
    assert d["tailor"]["model"] and "refilter" in d["tasks"] and d["task"]["running"] is False

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "scheduled_run.log").write_text("\n".join(f"line {i}" for i in range(500)))
    text = client.get("/api/logs/scheduled", params={"lines": 50}).json()["text"]
    assert text.splitlines() == [f"line {i}" for i in range(450, 500)]
    assert client.get("/api/logs/secrets").status_code == 404           # only the known logs
    assert client.post("/api/tasks/rm").status_code == 404              # only the known commands
    assert client.post("/api/run", json={"mode": "nope"}).status_code == 400


def test_run_modes_pass_flags(monkeypatch, tmp_path):
    from jobagent import web
    calls = []

    class P:
        def __init__(self, args, **kw): calls.append(args)
        def poll(self): return 0

    monkeypatch.setattr(web, "ROOT", tmp_path)
    monkeypatch.setattr(web.subprocess, "Popen", P)
    monkeypatch.setattr(web, "_run_proc", None)
    client = TestClient(web.app)
    assert client.post("/api/run", json={"mode": "collect"}).json()["ok"]
    assert calls[-1][-3:] == ["run", "--no-score", "--no-tailor"]
    assert client.post("/api/run", json={}).json()["ok"]                # old dashboard button: full run
    assert calls[-1][-1] == "run"
    assert client.post("/api/tasks/refilter").json()["ok"]
    assert calls[-1][-1] == "refilter"


def test_claude_login_status_and_test(cfg, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from jobagent import claude_cli, web
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout='{"loggedIn": true, "authMethod": "oauth_token"}', returncode=0))
    web._auth_cache.clear()
    client = TestClient(web.app)
    lg = client.get("/api/system").json()["login"]
    assert lg["logged_in"] and "CLAUDE_CODE_OAUTH_TOKEN" in lg["label"] and lg["last_ok"] is None

    def bad(*a, **k):
        claude_cli.last_usage.clear(); claude_cli.last_usage.update(model="claude-haiku-5-5", ok=False)
        raise claude_cli.ClaudeError("Authentication error")
    monkeypatch.setattr(claude_cli, "run", bad)
    r = client.post("/api/system/claude-test").json()
    assert not r["ok"] and "Authentication error" in r["message"]

    def good(*a, **k):
        claude_cli.last_usage.clear(); claude_cli.last_usage.update(model="claude-haiku-5-5", cost_usd=0.001)
        return "ok"
    monkeypatch.setattr(claude_cli, "run", good)
    assert client.post("/api/system/claude-test").json() == {"ok": True, "message": "ok"}
    lg = client.get("/api/system").json()["login"]
    assert lg["last_ok"]["model"] == "claude-haiku-5-5" and lg["last_fail"]      # both calls logged


G2I = ("Contract | Remote, worldwide | $100-$200/hour | 10-20 hrs/week. We're looking for senior engineers to help "
       "evaluate the quality of interactions with modern coding agents. This is not a traditional engineering role.")
XPERTEEZ = ("Role Type: Contract (20 hrs per week). Python, Java. In this role, you'll apply your expertise to help "
            "train next-generation AI systems.")
EMETLAB = ("Design and build the systems that power elite AI training data infrastructure for frontier labs. "
           "Full-Time and Part-Time positions available. Java, Python, Kafka. 10+ years.")


def test_gig_rules():
    assert rules.gig_reason("Python Senior Software Engineer, AI Training", "") .startswith("title")
    assert rules.gig_reason("Software Engineer, AI Coding Tasks (Contract)", "")
    assert rules.gig_reason("Senior Backend Engineer", XPERTEEZ).startswith("description")
    assert rules.gig_reason("Senior Software Engineer", G2I)
    # real jobs: building AI-training infrastructure full time, an AI data engineer, ML infra
    assert rules.gig_reason("Senior Software Engineer", EMETLAB) is None
    assert rules.gig_reason("AI Data Engineer", "Build pipelines. Full-time.") is None
    assert rules.gig_reason("Senior Backend Engineer", "We train models on GPUs; Kafka, Java. Full-time.") is None


def test_gigs_tagged_kept_out_of_strong_and_auto_tailor(cfg, monkeypatch):
    jobs = [RawJob("himalayas", "Python Senior Software Engineer, AI Training", "G2i", "Remote (Worldwide)",
                   url="https://h/1", remote=True, description=G2I + " Visa sponsorship available."),
            RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai", url="https://li/9", description=DESC)]
    gig_first = lambda prompt: json.dumps([{"id": int(i), "score": 95, "reasons": [], "red_flags": []}
                                           for i in re.findall(r"### id=(\d+)", prompt)])
    stats = pipeline.run(cfg, collector_overrides={"fake": lambda c, r: jobs}, scorer_llm=gig_first,
                         tailor_llm=make_fake_tailor(__import__("pathlib").Path(cfg["paths"]["resume_workspace"])))
    conn = db.connect(cfg.path("db"))
    g2i = conn.execute("SELECT * FROM jobs WHERE company='G2i'").fetchone()
    deriv = conn.execute("SELECT * FROM jobs WHERE company='Deriv'").fetchone()
    assert g2i["gig"] == 1 and g2i["status"] == "active" and deriv["gig"] == 0        # kept, tagged
    assert g2i["tailor_status"] == "none" and deriv["tailor_status"] == "done"       # gigs never auto-tailored
    assert stats["gigs"] == 1

    strong = db.list_tab(conn, "active", min_score=70)
    assert [r["company"] for r in strong] == ["Deriv"]                               # not in strong matches
    assert [r["company"] for r in db.list_tab(conn, "active", gigs_only=True)] == ["G2i"]
    assert [r["company"] for r in db.list_tab(conn, "active", gigs_only=True, min_score=70)] == ["G2i"]
    assert db.tab_counts(conn)["gigs"] == 1

    from jobagent import web
    monkeypatch.setattr(web, "load_config", lambda: cfg)
    data = TestClient(web.app).get("/api/jobs", params={"tab": "active", "gigs_only": "true"}).json()
    assert [j["company"] for j in data["jobs"]] == ["G2i"] and data["jobs"][0]["gig"] == 1


def test_scorer_can_flag_a_gig_the_rules_missed(cfg):
    conn = db.connect(cfg.path("db"))
    jid, _ = db.upsert_job(conn, RawJob("x", "Senior Software Engineer", "Outlier", "Remote", url="https://o/1",
                                        description="Write prompts. Contract."), "fp-o", 1)
    db.set_score(conn, jid, 70, [], [], gig=True)
    assert db.get_job(conn, jid)["gig"] == 2
    pipeline.mark_gigs(conn)                                   # keyword pass doesn't undo the scorer's tag
    assert db.get_job(conn, jid)["gig"] == 2


def test_company_exclude_drops_jobgether(cfg):
    cfg["filter"]["company_exclude"] = ["jobgether"]
    jobs = [RawJob("linkedin", "Senior Python Developer", "Jobgether", "Dubai", url="https://li/jg", description=DESC),
            RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai", url="https://li/1", description=DESC)]
    run(cfg, jobs)
    conn = db.connect(cfg.path("db"))
    jg = conn.execute("SELECT status, filter_reason FROM jobs WHERE company='Jobgether'").fetchone()
    assert jg["status"] == "filtered" and "company excluded" in jg["filter_reason"]


def test_old_database_gets_new_column(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.executescript(db.SCHEMA.replace(",\n    gig             INTEGER NOT NULL DEFAULT 0   -- AI-training gig: 0 no, 1 by rule, 2 by the scorer", ""))
    old.execute("INSERT INTO jobs(fingerprint, company, title, first_seen, last_seen) VALUES ('f','C','T','x','x')")
    old.commit(); old.close()
    conn = db.connect(p)
    assert conn.execute("SELECT gig FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("title, keep", [
    ("Senior Engineer - Software Development", True),
    ("Senior Backend & Platform Engineer", True),
    ("Senior/Lead/Staff Backend Engineers", True),
    ("Engineer, Software (Payments)", True),
    ("Senior Full-Stack Product Engineer", True),
    ("Software Development Officer", False),            # no "engineer"
    ("Senior Platform Engineer (Infrastructure)", False),
    ("Backend Engineering Manager", False),             # "engineering" isn't "engineer"; manager excluded anyway
    ("Java Technical Architect", True),
    ("Backend Architect, Binance Pay & Card", True),
    ("Forward Deployed AI Engineer", True),
    ("Senior Agentic AI Engineer", True),
    ("Sr Solutions Architect", False),                  # pre-sales, left out
    ("Senior DevOps Engineer Azure Platform IaC", False),
    ("Data Engineer", False),
])
def test_title_words_in_any_order(cfg, title, keep):
    raw = RawJob("linkedin", title, "Acme", "Dubai, UAE", description=DESC)
    assert rules.check(raw, cfg["filter"])[0] is keep


def test_no_longer_available_skips_dont_train_the_scorer(cfg):
    conn = db.connect(cfg.path("db"))
    for i, reason in enumerate(["Wrong stack", "Job no longer available", "expired on LinkedIn", "Position filled",
                                "Too junior"]):
        jid, _ = db.upsert_job(conn, RawJob("x", f"Engineer {i}", f"Co{i}", "Dubai", url=f"https://x/{i}"), f"fp{i}", 1)
        db.set_status(conn, jid, "skipped", reason)
    fb = db.skip_feedback(conn)
    assert sorted(f.split(": ")[1] for f in fb) == ["Too junior", "Wrong stack"]
    assert "Job no longer available" in TestClient(__import__("jobagent.web", fromlist=["app"]).app).get("/").text
