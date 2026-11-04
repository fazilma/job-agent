from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import ROOT, load_config


def _setup_logging(verbose: bool) -> None:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logs / "jobagent.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_run(args, cfg):
    from .pipeline import NoNetwork, run
    try:
        stats = run(cfg, sources=args.only or None, skip_score=args.no_score, skip_tailor=args.no_tailor)
    except NoNetwork:
        sys.exit("No internet connection, so the run was skipped. Try again once you're online.")
    print(json.dumps(stats, indent=2))


def cmd_serve(args, cfg):
    import uvicorn
    host = args.host or cfg.section("dashboard", "host", default="127.0.0.1")
    port = args.port or cfg.section("dashboard", "port", default=8765)
    if args.daily:
        from . import daily, web
        daily.start(daily.parse_at(args.daily), cfg.path("db"), lambda: web.start_run("scheduled_run.log"))
    print(f"Dashboard: http://{host}:{port}")
    uvicorn.run("jobagent.web:app", host=host, port=port, log_level="warning")


def cmd_check(args, cfg):
    from .collectors.ats import check_companies
    ok = 0
    for name, token, kind, n in check_companies(cfg):
        mark = "OK " if kind else "-- "
        ok += bool(kind)
        print(f"{mark}{name:<24} {token:<24} {kind or 'not found':<11} {n} jobs")
    print(f"\n{ok} resolved. Unresolved ones: fix the token (from the company's careers URL) or remove them.")


def cmd_tailor(args, cfg):
    from . import db, tailor
    conn = db.connect(cfg.path("db"))
    row = db.get_job(conn, args.job_id)
    if not row:
        sys.exit(f"no job {args.job_id}")
    ok = tailor.tailor_one(conn, cfg, row)
    print("done" if ok else f"failed: {db.get_job(conn, args.job_id)['tailor_error']}")


def cmd_check_agencies(args, cfg):
    """Run each agency scraper once and report what it found (nothing is saved)."""
    import time
    from .collectors import agencies
    for site in args.sites or list(agencies.SITES):
        t0 = time.time()
        jobs = agencies.collect(cfg, None, sites=[site])
        with_desc = sum(1 for j in jobs if len(j.description or "") > 200)
        print(f"{site:<13} {len(jobs):>4} jobs, {with_desc} with full description  ({time.time() - t0:.0f}s)")
        for j in jobs[:3]:
            print(f"      - {j.title[:60]} | {j.location or 'no location'}")
    print("\n0 jobs for a site usually means its page layout changed; check logs/jobagent.log.")


def cmd_apify_usage(args, cfg):
    from .collectors import apify_boards as ab
    u = ab.account_usage()
    if not u:
        sys.exit("Couldn't read your Apify usage (check APIFY_TOKEN in .env and your connection).")
    plan = ab.budget_plan(cfg, u)
    print(f"Apify this cycle: {plan['used']:.2f} of {plan['budget']:.2f} USD used, resets {plan['cycle_end'][:10]}")
    print(f"Left after the {cfg.section('apify', 'reserve_usd', default=0.25)} USD reserve: {max(plan['left'], 0):.2f} USD "
          f"over {plan['days']:.1f} days -> {plan['today']:.3f} USD per run")
    for src, usd in plan["per_search"].items():
        c = cfg.section("sources", src, default={}) or {}
        conf = int(c.get("limit_per_search") or c.get("max_results") or 40)
        print(f"  {src:<11} {usd:.4f} USD per search -> up to {ab.results_for(src, usd, conf)} results "
              f"x {len(c.get('searches', []))} searches")


def _short_model(name) -> str:
    """claude-sonnet-5-5 -> sonnet-5-5"""
    return (name or "?").removeprefix("claude-")


def cmd_claude_usage(args, cfg):
    from . import db
    conn = db.connect(cfg.path("db"))
    rows = db.claude_usage_by_day(conn, args.days)
    if not rows:
        sys.exit("No Claude calls logged yet (logging starts with the next score/tailor run).")
    print(f"Claude usage, last {args.days} days (cost = Claude Code's estimate at API prices;")
    print("your plan counts the same tokens against its limit, so use it to compare, not as a bill)\n")
    print(f"{'day':<11} {'what':<7} {'model':<11} {'calls':>5} {'est. USD':>9} {'tokens in':>11} {'out':>8} "
          f"{'turns':>6} {'minutes':>8}")
    for r in rows:
        print(f"{r['day']:<11} {r['purpose']:<7} {_short_model(r['model']):<11} {r['calls']:>5} {r['cost'] or 0:>9.2f} "
              f"{r['tokens_in'] or 0:>11,} "
              f"{r['tokens_out'] or 0:>8,} {r['turns'] or 0:>6} {(r['secs'] or 0) / 60:>8.1f}")
    top = db.costliest_tailorings(conn, args.days)
    if top:
        print("\nMost expensive tailorings:")
        for r in top:
            print(f"  {r['cost_usd'] or 0:6.2f} USD  {_short_model(r['model']):<11} {r['turns'] or 0:>3} turns  "
                  f"{(r['secs'] or 0) / 60:4.1f} min  "
                  f"{(r['company'] or '?')[:25]} - {(r['title'] or '')[:40]}")


def cmd_rescore(args, cfg):
    from . import db, scorer
    conn = db.connect(cfg.path("db"))
    n = scorer.clear_scores(conn)
    print(f"cleared {n} scores; re-scoring...")
    print(f"scored {scorer.score_pending(conn, cfg)}")


def cmd_compare_scores(args, cfg):
    from . import db, scorer
    conn = db.connect(cfg.path("db"))
    old_model = cfg.section("scorer", "model", default="haiku")
    print(f"Re-scoring the {args.limit} most recently scored jobs with {args.model} "
          f"(nothing is saved; scores in the dashboard stay as they are)...\n", flush=True)
    results = scorer.compare(conn, cfg, model=args.model, limit=args.limit)
    report = scorer.compare_report(results, cfg.section("tailor", "threshold", default=80), old_model, args.model)
    print(report)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "score_compare.txt").write_text(report + "\n", encoding="utf-8")
    print("\nSaved to logs/score_compare.txt")


def cmd_refilter(args, cfg):
    from . import db
    from .pipeline import refilter
    conn = db.connect(cfg.path("db"))
    print(json.dumps(refilter(conn, cfg), indent=2))


def cmd_schedule(args, cfg):
    from .schedule import install
    print(install(hour=args.hour, minute=args.minute))


def cmd_unschedule(args, cfg):
    from .schedule import uninstall
    print(uninstall())


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobagent")
    p.add_argument("-c", "--config")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="collect, filter, score, tailor (the daily job)")
    r.add_argument("--only", nargs="*", help="limit to sources: linkedin bayt naukrigulf career_pages agencies himalayas remotive workingnomads nodesk jobspresso remoterocketship web_search")
    r.add_argument("--no-score", action="store_true")
    r.add_argument("--no-tailor", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("serve", help="open the dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--daily", metavar="HH:MM", default=os.environ.get("JOBAGENT_DAILY_AT") or None,
                   help="also run the pipeline every day at this local time (used by Docker)")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("check-companies", help="verify career-page tokens in config.yaml")
    c.set_defaults(fn=cmd_check)

    t = sub.add_parser("tailor", help="tailor a resume for one job id now")
    t.add_argument("job_id", type=int)
    t.set_defaults(fn=cmd_tailor)

    ca = sub.add_parser("check-agencies", help="test the recruitment-agency scrapers (saves nothing)")
    ca.add_argument("sites", nargs="*", help="e.g. hays salt (default: all)")
    ca.set_defaults(fn=cmd_check_agencies)

    au = sub.add_parser("apify-usage", help="show Apify spend this cycle and today's per-search budget")
    au.set_defaults(fn=cmd_apify_usage)

    cu = sub.add_parser("claude-usage", help="Claude calls, tokens and estimated cost per day")
    cu.add_argument("--days", type=int, default=7)
    cu.set_defaults(fn=cmd_claude_usage)

    rs = sub.add_parser("rescore", help="re-score all Active jobs (after changing what you want)")
    rs.set_defaults(fn=cmd_rescore)

    cs = sub.add_parser("compare-scores", help="re-score recent jobs with another model and compare (saves nothing)")
    cs.add_argument("--model", default="sonnet")
    cs.add_argument("--limit", type=int, default=25)
    cs.set_defaults(fn=cmd_compare_scores)

    rf = sub.add_parser("refilter", help="re-apply config.yaml filter rules to jobs already in Active")
    rf.set_defaults(fn=cmd_refilter)

    sc = sub.add_parser("schedule", help="install the daily macOS launchd job")
    sc.add_argument("--hour", type=int, default=7)
    sc.add_argument("--minute", type=int, default=30)
    sc.set_defaults(fn=cmd_schedule)

    us = sub.add_parser("unschedule", help="remove the macOS launchd job (e.g. after moving to Docker)")
    us.set_defaults(fn=cmd_unschedule)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
