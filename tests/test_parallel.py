import time

import pytest

from jobagent import parallel, pipeline


def test_runs_side_by_side():
    t0 = time.time()
    res = parallel.run_all({f"t{i}": (lambda i=i: (time.sleep(0.4), i)[1]) for i in range(4)}, max_workers=4)
    assert time.time() - t0 < 1.0                                   # ~0.4s, not 1.6s
    assert sorted(r for r, _ in res.values()) == [0, 1, 2, 3]


def test_errors_are_returned_not_raised():
    res = parallel.run_all({"ok": lambda: 1, "bad": lambda: 1 / 0})
    assert res["ok"][0] == 1 and isinstance(res["bad"][0], ZeroDivisionError)


def test_hung_task_is_abandoned():
    t0 = time.time()
    res = parallel.run_all({"fast": lambda: "done", "hung": lambda: time.sleep(30)}, deadline_s=0.5)
    assert time.time() - t0 < 2
    assert res["fast"][0] == "done" and isinstance(res["hung"][0], parallel.Abandoned)


def test_pipeline_collects_in_parallel_and_skips_hung_source(tmp_path):
    import json
    from jobagent.config import Config, load_config
    from jobagent.models import RawJob
    cfg = Config(json.loads(json.dumps(load_config())))
    cfg["paths"]["db"] = str(tmp_path / "jobs.db")
    cfg["paths"]["raw_dir"] = str(tmp_path / "raw")
    cfg["run"]["collect_timeout_minutes"] = 0.02                    # ~1.2s
    def slow(c, r):
        time.sleep(0.5)
        return [RawJob("x", "Senior Backend Engineer", "A", "Dubai", url="https://a/1")]
    def slow2(c, r):
        time.sleep(0.5)
        return [RawJob("y", "Senior Software Engineer", "B", "Dubai", url="https://b/1")]
    def hung(c, r):
        time.sleep(30)
    t0 = time.time()
    stats = pipeline.run(cfg, collector_overrides={"a": slow, "b": slow2, "hung": hung},
                         skip_score=True, skip_tailor=True)
    assert time.time() - t0 < 3
    assert stats["new"] == 2 and "still running" in stats["sources"]["hung"]["error"]


def test_no_network_skips_run(tmp_path, monkeypatch):
    import json, socket
    from jobagent.config import Config, load_config
    cfg = Config(json.loads(json.dumps(load_config())))
    cfg["paths"]["db"] = str(tmp_path / "jobs.db")
    cfg["run"]["network_wait_seconds"] = 0
    def no_dns(*a, **k):
        raise socket.gaierror(8, "nodename nor servname provided, or not known")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(pipeline.NoNetwork):
        pipeline.run(cfg)
    assert not (tmp_path / "jobs.db").exists()                      # nothing touched
