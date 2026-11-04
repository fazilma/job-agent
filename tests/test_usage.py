"""Every Claude call is logged with its cost estimate and tokens."""
import json
import re
import subprocess
from pathlib import Path

import pytest

from jobagent import claude_cli, db, scorer, tailor
from jobagent.config import Config, load_config
from jobagent.models import RawJob


def cli_payload(cost, turns=1, result="ok", is_error=False, model="claude-sonnet-5-5"):
    return json.dumps({"type": "result", "is_error": is_error, "result": result, "total_cost_usd": cost,
                       "num_turns": turns, "usage": {"input_tokens": 100, "output_tokens": 2000,
                       "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 8000},
                       "modelUsage": {model: {}}})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = Config(json.loads(json.dumps(load_config())))
    ws = tmp_path / "Resume"
    (ws / "Backend_2026").mkdir(parents=True)
    (ws / "Backend_2026" / "Fazil_Backend_9Yrs.md").write_text("base")
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(ws), "base_resume": str(ws / "Backend_2026" / "Fazil_Backend_9Yrs.md")}
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "claude")
    return c


def add_job(conn, title="Senior Backend Engineer", company="Deriv"):
    jid, _ = db.upsert_job(conn, RawJob("linkedin", title, company, "Dubai", description="Java"), f"fp-{company}", 1)
    conn.commit()
    return jid


def test_tailoring_and_scoring_calls_are_logged(cfg, monkeypatch):
    conn = db.connect(cfg.path("db"))
    jid = add_job(conn)
    ws = Path(cfg["paths"]["resume_workspace"])

    def fake_run(cmd, input, **kw):
        if "haiku" in cmd:
            ids = [int(x) for x in re.findall(r"### id=(\d+)", input)]
            res = json.dumps([{"id": i, "score": 90, "reasons": [], "red_flags": []} for i in ids])
            return subprocess.CompletedProcess(cmd, 0, stdout=cli_payload(0.01, 1, res, model="claude-haiku-5-5"), stderr="")
        out = ws / "deriv_senior-backend-engineer"
        out.mkdir(exist_ok=True)
        (out / "Fazil_Resume.pdf").write_bytes(b"%PDF")
        return subprocess.CompletedProcess(cmd, 0, stdout=cli_payload(0.42, 18, f"done\nOUTPUT_DIR={out}"), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert scorer.score_pending(conn, cfg) == 1
    assert tailor.tailor_one(conn, cfg, db.get_job(conn, jid))
    rows = {r["purpose"]: dict(r) for r in conn.execute("SELECT * FROM claude_usage")}
    assert rows["score"]["cost_usd"] == 0.01 and rows["score"]["model"] == "claude-haiku-5-5"
    t = rows["tailor"]
    assert t["cost_usd"] == 0.42 and t["turns"] == 18 and t["job_id"] == jid and t["ok"] == 1
    assert t["cache_read_tokens"] == 50000 and t["output_tokens"] == 2000
    by_day = db.claude_usage_by_day(conn)
    assert {r["purpose"]: r["calls"] for r in by_day} == {"score": 1, "tailor": 1}
    assert db.costliest_tailorings(conn)[0]["company"] == "Deriv"


def test_failed_and_limited_calls_are_logged_too(cfg, monkeypatch):
    conn = db.connect(cfg.path("db"))
    jid = add_job(conn)
    payload = json.dumps({"is_error": True, "api_error": "usage_limit_reached", "total_cost_usd": 0.49,
                          "num_turns": 10, "result": "You've hit your session limit", "usage": {}})
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout=payload, stderr=""))
    with pytest.raises(claude_cli.UsageLimitError):
        tailor.tailor_one(conn, cfg, db.get_job(conn, jid))
    r = conn.execute("SELECT * FROM claude_usage").fetchone()
    assert r["purpose"] == "tailor" and r["ok"] == 0 and r["cost_usd"] == 0.49     # half-done work still counted


def test_claude_usage_command(cfg, capsys, monkeypatch, tmp_path):
    import yaml
    from jobagent import cli
    conn = db.connect(cfg.path("db"))
    jid = add_job(conn)
    db.log_claude_usage(conn, "tailor", {"cost_usd": 0.42, "turns": 18, "secs": 270, "model": "sonnet"}, jid)
    db.log_claude_usage(conn, "score", {"cost_usd": 0.01, "turns": 1, "secs": 30, "model": "haiku"})
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(dict(cfg)))
    cli.main(["-c", str(p), "claude-usage"])
    out = capsys.readouterr().out
    assert "tailor" in out and "0.42" in out and "Deriv" in out and "Most expensive tailorings" in out
    assert "model" in out and "sonnet" in out and "haiku" in out
    db.log_claude_usage(conn, "tailor", {"cost_usd": 0.9, "model": "claude-opus-5-5"}, jid)
    cli.main(["-c", str(p), "claude-usage"])
    out = capsys.readouterr().out
    assert "opus-5-5" in out and "claude-opus" not in out          # one row per model, short name


def test_compare_scores_saves_nothing(cfg, capsys, tmp_path, monkeypatch):
    import yaml
    from jobagent import cli, scorer
    conn = db.connect(cfg.path("db"))
    ids = []
    for i, (company, old) in enumerate([("Deriv", 75), ("Acme", 60), ("Zeta", 90)]):
        jid, _ = db.upsert_job(conn, RawJob("linkedin", "Senior Backend Engineer", company, "Dubai",
                                            url=f"https://x/{i}", description="Java"), f"fp{i}", 1)
        db.set_score(conn, jid, old, [f"{company} haiku reason"], [])
        ids.append(jid)
    conn.commit()
    new = {ids[0]: 88, ids[1]: 62, ids[2]: 70}

    def fake(prompt):
        got = [int(x) for x in re.findall(r"### id=(\d+)", prompt)]
        return json.dumps([{"id": i, "score": new[i], "reasons": ["sonnet says"]} for i in got])

    res = scorer.compare(conn, cfg, llm=fake)
    assert [r["company"] for r in res] == ["Zeta", "Deriv", "Acme"]          # biggest disagreement first
    assert db.get_job(conn, ids[0])["score"] == 75                           # nothing saved
    report = scorer.compare_report(res, 80, "haiku", "sonnet")
    assert "1 job(s) would newly qualify, 1 would drop out" in report
    assert "would auto-tailor" in report and "Zeta haiku reason" in report and "sonnet says" in report
    assert "Deriv haiku reason" not in report                                # 13 points: under the 15 cutoff
