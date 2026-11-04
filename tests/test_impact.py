import json
import re
from pathlib import Path

import pytest

from jobagent import db, impact, pipeline, tailor
from jobagent.config import Config, load_config
from jobagent.models import RawJob

CR = {"role": "CoinRoutes", "bullet": "Built the order-routing service for institutional clients",
      "questions": ["Roughly how many orders per day?", "Did latency change? From what to what?"]}
KAT = {"role": "Katim", "bullet": "Maintained the device management backend",
       "questions": ["How many devices did it manage?"]}


@pytest.fixture
def cfg(tmp_path):
    c = Config(json.loads(json.dumps(load_config())))
    ws = tmp_path / "Resume"
    (ws / "Backend_2026").mkdir(parents=True)
    (ws / "Backend_2026" / "Fazil_Backend_9Yrs.md").write_text("base resume")
    c["paths"] = {"db": str(tmp_path / "jobs.db"), "raw_dir": str(tmp_path / "raw"),
                  "resume_workspace": str(ws), "base_resume": str(ws / "Backend_2026" / "Fazil_Backend_9Yrs.md")}
    return c


def answer(text, qid, value):
    """Type an answer under one entry, the way you'd do it in an editor."""
    start = text.index(f"### {qid}")
    i = text.index("**Answer:**\n", start) + len("**Answer:**\n")
    return text[:i] + value + "\n" + text[i:]


def test_same_bullet_from_many_jobs_is_asked_once(cfg):
    impact.record(cfg, [CR, KAT], "Deriv - Senior Backend Engineer")
    impact.record(cfg, [dict(CR, questions=["Roughly how many orders per day?", "What fill rate?"])],
                  "Huspy - Backend Engineer")
    entries = impact.load(cfg)
    assert [e.id for e in entries] == ["Q-0001", "Q-0002"]
    cr = entries[0]
    assert cr.asked_by == ["Deriv - Senior Backend Engineer", "Huspy - Backend Engineer"]
    assert len(cr.questions) == 3                                    # merged, no duplicate wording


def test_your_answers_survive_and_feed_the_next_tailoring(cfg):
    impact.record(cfg, [CR, KAT], "Deriv - Senior Backend Engineer")
    p = impact.path(cfg)
    text = answer(p.read_text(), "Q-0001", "About 40,000 orders a day; p99 latency went from 40 ms to 12 ms.")
    p.write_text(answer(text, "Q-0002", "SKIP"))                   # what you'd type in the file

    # a later tailoring adds a new question; your answers must stay put
    impact.record(cfg, [dict(CR, questions=["Any other metric?"]),
                        {"role": "Dubizzle", "bullet": "Owned search APIs", "questions": ["QPS?"]}], "Job 2")
    entries = {e.id: e for e in impact.load(cfg)}
    assert entries["Q-0001"].answered and "40 ms to 12 ms" in entries["Q-0001"].answer
    assert entries["Q-0001"].questions == CR["questions"]           # answered: not re-asked
    assert entries["Q-0002"].skipped and not entries["Q-0002"].answered
    assert entries["Q-0003"].role == "Dubizzle" and not entries["Q-0003"].answer

    facts = impact.confirmed_facts_block(list(entries.values()))
    assert "40 ms to 12 ms" in facts and "Katim" not in facts      # SKIP is never a fact
    rendered = impact.path(cfg).read_text()
    assert rendered.index("## Waiting for your answer") < rendered.index("## Answered") < rendered.index("## Skipped")
    assert "**1 waiting** · 1 answered · 1 skipped" in rendered


def test_blank_answer_is_waiting_not_answered(cfg):
    impact.record(cfg, [CR], "Deriv")
    e = impact.load(cfg)[0]
    assert e.answer == "" and not e.answered and not e.skipped


def test_extract_questions_block():
    reply = 'Report...\n```impact-questions\n[{"role": "Katim", "bullet": "x", "questions": ["y"]}]\n```\nOUTPUT_DIR=/a'
    assert impact.extract_questions(reply) == [{"role": "Katim", "bullet": "x", "questions": ["y"]}]
    assert impact.extract_questions("no block") == []
    assert impact.extract_questions("```impact-questions\nnot json\n```") == []


def test_prompt_carries_impact_pass_and_confirmed_facts(cfg):
    impact.record(cfg, [CR, KAT], "Deriv")
    p = impact.path(cfg)
    p.write_text(answer(p.read_text(), "Q-0001", "40,000 orders a day"))
    conn = db.connect(cfg.path("db"))
    raw = RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai", description="Java")
    jid, _ = db.upsert_job(conn, raw, "fp1", 1)
    prompt = tailor.build_prompt(cfg, db.get_job(conn, jid))
    assert "anthropic-skills:resume-impact-pass" in prompt
    assert "Impact pass on that content before writing it" in prompt
    assert "ONE Write call" in prompt and "no pdftoppm" in prompt
    assert "no verification scorecard" in prompt and "Do NOT\n   write the tailored Markdown" in prompt
    assert "already saved" in prompt and "```application-answers" in prompt
    assert "40,000 orders a day" in prompt                          # answered -> confirmed fact
    assert 'Katim: "Maintained the device management backend"' in prompt   # already asked
    assert "```impact-questions" in prompt and "Do NOT edit\n   Backend_2026/" in prompt


def test_tailoring_writes_questions_to_shared_file(cfg):
    ws = Path(cfg["paths"]["resume_workspace"])

    def fake(prompt, cwd):
        company = re.search(r"Company: (.+)", prompt).group(1).strip()
        out = ws / company.replace(" ", "_")
        out.mkdir(exist_ok=True)
        (out / "Fazil_Resume.pdf").write_bytes(b"%PDF fake")
        return ("Done.\n```impact-questions\n" + json.dumps([CR]) + "\n```\n" + f"OUTPUT_DIR={out}")

    def scorer(prompt):
        ids = [int(x) for x in re.findall(r"### id=(\d+)", prompt)]
        return json.dumps([{"id": i, "score": 95, "reasons": [], "red_flags": []} for i in ids])

    jobs = [RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai", url="https://a/1", description="x"),
            RawJob("linkedin", "Senior Software Engineer", "Huspy", "Dubai", url="https://a/2", description="y")]
    stats = pipeline.run(cfg, collector_overrides={"f": lambda c, r: jobs}, scorer_llm=scorer, tailor_llm=fake)
    assert stats["tailored"] == 2
    entries = impact.load(cfg)
    assert len(entries) == 1 and len(entries[0].asked_by) == 2      # one bullet, asked by both jobs


def test_folder_jd_and_answers_written_without_claude(cfg):
    cfg["application_profile"] = {"notice_period": "1 month", "current_location": "Dubai, UAE"}
    conn = db.connect(cfg.path("db"))
    raw = RawJob("linkedin", "Senior Backend Engineer", "Deriv", "Dubai", url="https://x/1",
                 description="We need Java and Kafka.")
    jid, _ = db.upsert_job(conn, raw, "fp1", 1)
    row = db.get_job(conn, jid)
    seen = {}

    def fake(prompt, cwd):
        folder = Path(cwd) / tailor.folder_name(row)
        seen["jd"] = (folder / "Job_Description.md").read_text()      # saved before Claude runs
        (folder / "Fazil_Resume.pdf").write_bytes(b"%PDF fake")
        return ("Report.\n```application-answers\n## Why this company\nBecause Kafka.\n```\n"
                "```impact-questions\n[]\n```\n" + f"OUTPUT_DIR={folder}")

    assert tailor.tailor_one(conn, cfg, row, llm=fake)
    assert "We need Java and Kafka." in seen["jd"] and "https://x/1" in seen["jd"]
    done = db.get_job(conn, jid)
    ans = (Path(done["resume_dir"]) / "application_answers.md").read_text()
    assert done["answers"] == ans
    assert "Because Kafka." in ans and "**Notice period:** 1 month" in ans and "**GitHub:** [fill in]" in ans


def test_missing_jd_asks_claude_to_fetch(cfg):
    conn = db.connect(cfg.path("db"))
    jid, _ = db.upsert_job(conn, RawJob("linkedin", "Backend Engineer", "Acme", "Dubai", url="https://x/2"), "fp2", 1)
    prompt = tailor.build_prompt(cfg, db.get_job(conn, jid))
    assert "fetch the posting URL once" in prompt
