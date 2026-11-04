import threading
import time

from jobagent import db
from jobagent.collectors import apify_boards as ab
from jobagent.models import RawJob


def test_linkedin_copy_wins_over_bayt(tmp_path):
    conn = db.connect(tmp_path / "j.db")
    bayt = RawJob("bayt", "Senior Software Engineer - Java Full Stack", "Emirates Group", "Dubai, UAE",
                  url="https://bayt/75355178", apply_url="https://bayt/apply/75355178", description="Java")
    jid, _ = db.upsert_job(conn, bayt, "fp", 1)
    li = RawJob("linkedin", "Senior Software Engineer - Java Full Stack", "Emirates", "Dubai",
                url="https://linkedin/jobs/view/111", description="Java")
    db.upsert_job(conn, li, "fp", 2)
    j = db.get_job(conn, jid)
    assert j["primary_source"] == "linkedin" and j["url"] == j["apply_url"] == "https://linkedin/jobs/view/111"
    # seeing Bayt again doesn't switch it back
    db.upsert_job(conn, bayt, "fp", 3)
    assert db.get_job(conn, jid)["primary_source"] == "linkedin"
    # the company's own career page still beats LinkedIn for the apply link
    gh = RawJob("greenhouse", "Senior Software Engineer", "Emirates", "Dubai", url="https://gh/1",
                apply_url="https://gh/1/apply")
    db.upsert_job(conn, gh, "fp", 4)
    j = db.get_job(conn, jid)
    assert j["primary_source"] == "greenhouse" and j["apply_url"] == "https://gh/1/apply"
    db.upsert_job(conn, li, "fp", 5)
    assert db.get_job(conn, jid)["primary_source"] == "greenhouse"


def test_shared_apify_limit_puts_linkedin_first_and_retries(monkeypatch):
    running, peak, order, lock = [0], [0], [], threading.Lock()
    fails = {"bayt b1": 1}                                         # Apify says "full" once

    def fake_run_actor(actor, run_input, **kw):
        label = run_input["q"]
        with lock:
            if fails.get(label):
                fails[label] -= 1
                raise RuntimeError("By launching this job you will exceed your limit of 5 concurrent Actor runs.")
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            order.append(label)
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return []

    monkeypatch.setattr(ab, "run_actor", fake_run_actor)
    monkeypatch.setattr(ab, "save_raw", lambda *a: None)
    sleeps = []
    real_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s) if s >= 20 else real_sleep(s * 0.1))
    ab._gate = None
    threads = [threading.Thread(target=ab._collect, args=(src, "actor", [{"q": f"{src} {src[0]}{i}"} for i in range(4)], None),
                                kwargs={"parallel_runs": 3, "max_concurrent": 2})
               for src in ("bayt", "linkedin", "naukrigulf")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] <= 2                                            # never more than the shared limit
    assert len(order) == 12 and "bayt b1" in order                # the refused search was retried
    assert all(o.startswith("linkedin") for o in order[:2])       # LinkedIn went first
    assert 20 in sleeps                                            # waited before retrying
