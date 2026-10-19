from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .exporter import export_applications, export_mads_profile
from .scorer import score_job
from .sources.simplify import fetch as fetch_simplify, fetch_file as fetch_simplify_file
from .sources.jobspy_source import fetch as fetch_jobspy
from .store import connect, upsert_jobs, all_active_jobs, save_score, list_jobs, get_job, set_application
from .handoff import HandoffError, handoff_batch, import_result, prepare_handoff, select_queued_jobs
from .tailor import make_packet
from .evidence import validate_evidence_bank
from .jd import parse_job_description
from .resume_builder import auto_plan, build_application
from .profile import PROFILE_DIR, forbidden_resume_terms, required_resume_terms
from .validation import validate_pdf, render_preview
from .apply.engine import run_apply
from .apply.session import run_manual_session, signal_manual_session
from .batch import apply_batch, hydrate_job, prepare_batch
from .dashboard import write_dashboard
from .radar import fresh_report, run_watch, run_worker, sync_and_rank, sync_linkedin_and_rank

console = Console()
ROOT = Path(__file__).resolve().parents[2]


def config():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cmd_doctor(_):
    checks = {
        "python": sys.version.split()[0],
        "git": shutil.which("git") or "missing",
        "database": str((ROOT / "data" / "jobs.db")),
        "config": "ok" if (ROOT / "config.yaml").exists() else "missing",
        "profile": str(PROFILE_DIR) + (" (ok)" if (PROFILE_DIR / "profile.yaml").exists() else " (missing profile.yaml)"),
    }
    console.print(f"Job Beast v{__version__}")
    for k, v in checks.items():
        console.print(f"[bold]{k}[/bold]: {v}")


def cmd_init(_):
    with connect() as con:
        con.execute("SELECT 1")
    console.print("[green]Database initialized.[/green]")


def cmd_refresh(args):
    """Refresh the configured primary source without touching existing records on failure."""
    cmd_fetch_simplify(args)


def cmd_apply(args):
    if args.submit and args.dry_run:
        raise SystemExit("Choose either --dry-run or --submit, not both")
    try:
        result = run_apply(
            job_id=args.job_id,
            submit=args.submit,
            headed=args.headed and not args.headless,
            force=args.force,
            continue_run=args.continue_run,
            root=ROOT,
        )
    except Exception as exc:
        console.print(f"[red]Application preparation failed:[/red] {exc}")
        raise SystemExit(1)
    # Keep machine-readable output intact; Rich may wrap long paths otherwise.
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result["state"] in {"FAILED", "NEEDS_INPUT"}:
        raise SystemExit(2)


def cmd_browser_session(args):
    """Launch or signal the single persistent headed browser session."""
    if args.action:
        path = signal_manual_session(ROOT, args.action)
        print(json.dumps({"action": args.action, "signal": str(path)}))
        return
    try:
        asyncio.run(run_manual_session(job_id=args.job_id, root=ROOT))
    except KeyboardInterrupt:
        console.print("[yellow]Controlled browser watcher stopped.[/yellow]")
    except Exception as exc:
        console.print(f"[red]Controlled browser session failed:[/red] {exc}")
        raise SystemExit(1)


def cmd_hydrate(args):
    try:
        print(json.dumps(hydrate_job(args.job_id, force=args.force), indent=2))
    except Exception as exc:
        console.print(f"[red]Hydration failed:[/red] {exc}")
        raise SystemExit(1)


def cmd_batch_prepare(args):
    results = prepare_batch(limit=args.limit, min_score=args.min_score, hydrate=not args.no_hydrate, force=args.force, root=ROOT)
    print(json.dumps([item.as_dict() for item in results], indent=2, ensure_ascii=False))
    if any(item.state == "FAILED" for item in results):
        raise SystemExit(1)


def cmd_batch_apply(args):
    acfg = config().get("automation", {})
    results = apply_batch(
        limit=args.limit or acfg.get("per_run_limit", 25),
        min_score=args.min_score if args.min_score is not None else acfg.get("min_score"),
        submit=args.submit or bool(acfg.get("auto_submit", False)),
        headed=args.headed and not args.headless if args.headed else bool(acfg.get("headed", False)),
        daily_limit=args.daily_limit or int(acfg.get("daily_limit", 75)),
        root=ROOT,
    )
    print(json.dumps(results, indent=2, ensure_ascii=False))


def cmd_dashboard(args):
    path = write_dashboard(Path(args.output))
    console.print(f"[green]Dashboard written:[/green] {path}")


def cmd_fetch_simplify(args):
    cfg = config()
    scfg = cfg.get("simplify", {})
    try:
        common = dict(
            season=args.season or cfg.get("season", "Summer 2027"),
            active_only=scfg.get("active_only", True),
            visible_only=scfg.get("visible_only", True),
        )
        jobs = fetch_simplify_file(args.file, **common) if args.file else fetch_simplify(url=scfg.get("raw_url"), **common)
    except Exception as exc:
        console.print(f"[red]Simplify fetch failed:[/red] {exc}")
        console.print("Existing jobs remain intact. Retry later or use --file path/to/listings.json.")
        return
    ins, upd = upsert_jobs(jobs)
    console.print(f"Fetched {len(jobs)} Simplify listings. Inserted {ins}, refreshed {upd}.")


def cmd_fetch_jobspy(args):
    cfg = config().get("jobspy", {})
    try:
        jobs = fetch_jobspy(
            query=args.query,
            location=args.location or "",
            sites=args.sites or cfg.get("sites", ["indeed", "google", "zip_recruiter", "glassdoor"]),
            results=args.results,
            hours_old=args.hours_old,
            country=cfg.get("country", "usa"),
        )
    except Exception as exc:
        console.print(f"[red]JobSpy fetch failed:[/red] {exc}")
        console.print("Existing jobs remain intact; other sources can continue normally.")
        return
    ins, upd = upsert_jobs(jobs)
    console.print(f"Fetched {len(jobs)} JobSpy listings. Inserted {ins}, refreshed {upd}.")


def cmd_fresh(args):
    result = sync_and_rank(max_process=args.max_process, season=args.season or config().get("season", "Summer 2027"))
    print(json.dumps({"headline": "JOB BEAST FRESH RADAR", **result, "report": fresh_report(args.top)}, indent=2, ensure_ascii=False))


def linkedin_config(window_minutes: int | None = None) -> dict:
    cfg = config()
    lcfg = dict(cfg.get("apify_linkedin") or {})
    lcfg["titles"] = lcfg.get("titles") or cfg.get("search", {}).get("target_titles", [])
    if window_minutes:
        lcfg["window_minutes"] = window_minutes
    return lcfg


def _print_linkedin_report(result: dict, top: int) -> None:
    f, fresh = result["fetch"], result["fresh_counts"]
    print("LINKEDIN FETCH")
    print(f"  queries: {f['queries_attempted']} attempted, {f['queries_succeeded']} succeeded, "
          f"{f['queries_rate_limited']} rate-limited (data lost), {f['queries_failed']} failed; "
          f"LinkedIn 429 responses retried by the actor: {f['linkedin_429_responses']}")
    print(f"  raw listings: {f['raw_results']}")
    print(f"  off-target dropped: {f['off_target_dropped']}" + (f"  (e.g. {'; '.join(f['off_target_examples'][:5])})" if f["off_target_examples"] else ""))
    print(f"  distinct normalized jobs: {f['distinct_jobs']}")
    print(f"  new: {f['new']}   updated: {f['updated']}   duplicates: {f['duplicates']} (cross-source: {f['cross_source_duplicates']})")
    print(f"  ineligible: {f['ineligible']}   needs confirmation: {f['needs_confirmation']}")
    print("  Freshness (provable max age at fetch):")
    print(f"    within 1h: {fresh['<=1h']}   within 3h: {fresh['<=3h']}   within 12h: {fresh['<=12h']}")
    for q in f["queries"]:
        if q["status"] != "SUCCEEDED":
            print(f"  ! {q['status']}: {q['url']}" + (f" ({q['error']})" if q.get("error") else ""))
    rows = [p for p in result["processed"] if p.get("job_id") in set(result["job_ids"])]
    rows.sort(key=lambda p: (p.get("status") not in {"FAST_LANE", "QUEUED"}, -(p.get("priority") or 0)))
    print("  Jobs from this fetch:")
    for p in rows:
        fresh_p = p.get("freshness") or {}
        age = f"<= {fresh_p['max_age_hours']}h ({fresh_p.get('basis')})" if fresh_p.get("max_age_hours") is not None else "-"
        print(f"    [{p.get('status')}] {p.get('company', '')} | {p.get('title', '')} | fit {p.get('fit_score', '-')} | {age}" + (f" | {p.get('reason')}" if p.get("reason") else ""))
    print()
    print("OVERALL JOB BEAST QUEUE (all sources, not just this fetch)")
    for t in fresh_report(top)["top"]:
        print(f"  [{t['lane']}] {t['source']} | {t['company']} | {t['title']} | fit {t['fit_score']} | age {t['age_hours']}h ({t['age_basis']})")


def cmd_fetch_linkedin(args):
    lcfg = linkedin_config(args.window_minutes)
    if args.dry_run:
        from .sources.apify_linkedin import search_url
        for title in lcfg["titles"]:
            print(search_url(title, location=lcfg.get("location", "United States"), window_seconds=int(lcfg.get("window_minutes", 60)) * 60))
        return
    try:
        result = sync_linkedin_and_rank(max_process=args.max_process, cfg=lcfg)
    except Exception as exc:
        console.print(f"[red]LinkedIn (Apify) fetch failed:[/red] {exc}")
        console.print("Existing jobs remain intact; other sources can continue normally.")
        return
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        _print_linkedin_report(result, args.top)


def cmd_watch(args):
    def show(result):
        print(json.dumps({"headline": "JOB BEAST FRESH RADAR", **result, "report": fresh_report(args.top)}, ensure_ascii=False), flush=True)
    run_watch(interval_minutes=args.interval, once=args.once, max_process=args.max_process, callback=show,
              sources=tuple(args.sources), linkedin_cfg=linkedin_config() if "linkedin" in args.sources else None)


def cmd_worker(args):
    print(json.dumps(run_worker(limit=args.limit, submit=args.submit, headed=args.headed, root=ROOT), indent=2, ensure_ascii=False))


def cmd_rank(args):
    rows = all_active_jobs()
    for row in rows:
        report = score_job(row)
        save_score(row["id"], report["score"], report)
    console.print(f"Scored {len(rows)} active jobs.")
    _print_jobs(list_jobs(limit=args.top))


def _print_jobs(rows):
    table = Table(show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Company")
    table.add_column("Role")
    table.add_column("Location")
    table.add_column("Source")
    for r in rows:
        loc = ", ".join(json.loads(r["locations_json"] or "[]"))
        table.add_row(r["id"], "-" if r["fit_score"] is None else f"{r['fit_score']:.1f}", r["company"], r["title"], loc[:35], r["source"])
    console.print(table)


def cmd_list(args):
    _print_jobs(list_jobs(limit=args.limit, min_score=args.min_score))


def cmd_packet(args):
    row = get_job(args.job_id)
    if not row:
        raise SystemExit(f"Unknown job id: {args.job_id}")
    path = make_packet(row)
    console.print(f"[green]Packet created:[/green] {path}")


def cmd_queue(args):
    if not get_job(args.job_id):
        raise SystemExit(f"Unknown job id: {args.job_id}")
    set_application(args.job_id, args.status, args.notes or "")
    console.print(f"Queued {args.job_id} as {args.status}")


def cmd_handoff(args):
    try:
        result = prepare_handoff(args.job_id, force=args.force, root=ROOT, submission_authorized=args.authorize_submission)
    except HandoffError as exc:
        print(json.dumps({"job_id": args.job_id, "status": "FAILED", "reason": exc.reason, "detail": exc.detail}, indent=2, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    console.print(f"[green]{result['status']}[/green]: {result['company']} -- {result['title']}")
    console.print(f"  URL: {result['official_application_url']}")
    console.print(f"  Fit: {result['fit_score']}  Freshness: {result['freshness']['bucket']}")
    console.print(f"  Resume: {result['resume_path']}")
    console.print(f"  Context: {result['context_path']}")
    console.print(f"  Claude prompt: {result['prompt_path']}")


def cmd_handoff_batch(args):
    result = handoff_batch(limit=args.limit, root=ROOT, submission_authorized=args.authorize_submission)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    console.print(
        f"[bold]{result['attempted']} attempted[/bold] -- ready_for_claude={result['ready_for_claude']} "
        f"failed={result['failed']} ineligible={result['ineligible']} needs_confirmation={result['needs_confirmation']} "
        f"skipped_duplicate={result['skipped_duplicate']} closed={result['closed']}"
    )
    if result["ready_for_claude"] == 0 and result["attempted"] > 0:
        raise SystemExit(1)


def cmd_next(args):
    rows = select_queued_jobs(1, include_needs_input=args.include_needs_input)
    row = rows[0] if rows else None
    if not row:
        print(json.dumps({"status": "NO_JOB_AVAILABLE"}, indent=2))
        raise SystemExit(1)
    print(json.dumps({"job_id": row["job_id"], "company": row["company"], "title": row["title"], "lane": row["lane"], "queue_priority": row["queue_priority"]}, indent=2, ensure_ascii=False))


def cmd_operate(args):
    try:
        result = prepare_handoff(args.job_id, force=args.force, root=ROOT, submission_authorized=args.authorize_submission)
    except HandoffError as exc:
        print(json.dumps({"job_id": args.job_id, "status": "FAILED", "reason": exc.reason, "detail": exc.detail}, indent=2, ensure_ascii=False))
        raise SystemExit(1)
    console.print(Path(result["operator_instruction_path"]).read_text(encoding="utf-8"))


def cmd_operate_next(args):
    rows = select_queued_jobs(1, include_needs_input=args.include_needs_input)
    row = rows[0] if rows else None
    if not row:
        console.print("[yellow]No eligible job available for handoff right now.[/yellow]")
        raise SystemExit(1)
    args.job_id = row["job_id"]
    cmd_operate(args)


def cmd_import_result(args):
    try:
        result = import_result(args.job_id, root=ROOT)
    except HandoffError as exc:
        print(json.dumps({"job_id": args.job_id, "status": "FAILED", "reason": exc.reason, "detail": exc.detail}, indent=2, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_export(_):
    path = export_applications()
    console.print(f"[green]Exported:[/green] {path}")


def cmd_export_mads(_):
    path = export_mads_profile()
    console.print(f"[green]Mads-compatible candidate profile:[/green] {path}")


def _load_job_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_parse_jd(args):
    job = _load_job_file(args.job_file)
    parsed = parse_job_description(**job)
    out = Path(args.output or args.job_file).with_suffix(".parsed.json")
    out.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    console.print(f"[green]Parsed JD:[/green] {out}")


def cmd_tailor(args):
    job = _load_job_file(args.job_file)
    if args.dry_run:
        from .matching import build_requirement_matrix
        matrix = build_requirement_matrix(job)
        console.print(json.dumps({"job": job, "requirements": matrix["matches"], "coverage": matrix["coverage"]}, indent=2))
        return
    output_dir = Path(args.output_dir)
    plan = None
    result = build_application(job=job, output_dir=output_dir, plan=plan, tex_name=args.tex_name)
    pdf_path = Path(result["pdf"])
    if pdf_path.exists():
        validation = validate_pdf(pdf_path, required_terms=required_resume_terms(), forbidden_terms=forbidden_resume_terms())
        preview = render_preview(pdf_path, output_dir / f"{pdf_path.stem}_page1.png")
        result["validation"] = validation
        result["preview"] = str(preview) if preview else None
        (output_dir / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    console.print(json.dumps(result, indent=2))
    if not result.get("compiled") or result.get("validation", {}).get("errors"):
        raise SystemExit(1)


def cmd_coverage(args):
    """Regenerate an auditable JD/evidence report without rebuilding a resume."""
    job = _load_job_file(args.job_file)
    from .matching import build_requirement_matrix
    from .resume_builder import write_application_report
    matrix = build_requirement_matrix(job)
    plan = {"experiences": [], "projects": [], "skills": {}, "skill_evidence": {}}
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = write_application_report(job=job, matrix=matrix, plan=plan, output_path=output_dir / "requirement_coverage.json")
    console.print(json.dumps({"report": str(path), "coverage": matrix["coverage"]}, indent=2))


def cmd_validate_evidence(_):
    errors = validate_evidence_bank()
    if errors:
        for error in errors: console.print(f"[red]{error}[/red]")
        raise SystemExit(1)
    console.print("[green]Evidence bank valid.[/green]")


def parser():
    p = argparse.ArgumentParser(prog="jobbeast", description="Evidence-grounded internship search + application queue")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("init").set_defaults(func=cmd_init)

    refresh = sub.add_parser("refresh", help="Refresh configured job listings")
    refresh.add_argument("--season", default=None)
    refresh.add_argument("--file", default=None)
    refresh.set_defaults(func=cmd_refresh)

    f = sub.add_parser("fetch-simplify")
    f.add_argument("--season", default=None)
    f.add_argument("--file", default=None, help="Offline Simplify listings.json fallback")
    f.set_defaults(func=cmd_fetch_simplify)

    fresh = sub.add_parser("fresh", help="Sync fresh Summer 2027 listings, apply eligibility, rank, and queue")
    fresh.add_argument("--season", default=None)
    fresh.add_argument("--top", type=int, default=10)
    fresh.add_argument("--max-process", type=int, default=100)
    fresh.set_defaults(func=cmd_fresh)

    watch = sub.add_parser("watch", help="Continuously run the independent job radar")
    watch.add_argument("--interval", type=int, default=30)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--top", type=int, default=10)
    watch.add_argument("--max-process", type=int, default=100)
    watch.add_argument("--sources", nargs="+", choices=["simplify", "linkedin"], default=["simplify"])
    watch.set_defaults(func=cmd_watch)

    fl = sub.add_parser("fetch-linkedin", help="Fetch recent LinkedIn postings via Apify, then check eligibility, rank, and queue")
    fl.add_argument("--window-minutes", type=int, default=None, help="Only postings from the last N minutes (default: config)")
    fl.add_argument("--max-process", type=int, default=100)
    fl.add_argument("--top", type=int, default=10)
    fl.add_argument("--dry-run", action="store_true", help="Print the LinkedIn search URLs without calling Apify")
    fl.add_argument("--json", action="store_true", help="Print the full machine-readable result")
    fl.set_defaults(func=cmd_fetch_linkedin)

    worker = sub.add_parser("worker", help="Consume queued applications without stopping on one failure")
    worker.add_argument("--limit", type=int, default=1)
    worker.add_argument("--submit", action="store_true")
    worker.add_argument("--headed", action="store_true")
    worker.set_defaults(func=cmd_worker)

    j = sub.add_parser("fetch-jobspy")
    j.add_argument("--query", required=True)
    j.add_argument("--location", default="")
    j.add_argument("--sites", nargs="+")
    j.add_argument("--results", type=int, default=30)
    j.add_argument("--hours-old", type=int, default=168)
    j.set_defaults(func=cmd_fetch_jobspy)

    r = sub.add_parser("rank")
    r.add_argument("--top", type=int, default=25)
    r.set_defaults(func=cmd_rank)

    l = sub.add_parser("list")
    l.add_argument("--limit", type=int, default=25)
    l.add_argument("--min-score", type=float)
    l.set_defaults(func=cmd_list)

    a = sub.add_parser("packet")
    a.add_argument("job_id")
    a.set_defaults(func=cmd_packet)

    q = sub.add_parser("queue")
    q.add_argument("job_id")
    q.add_argument("--status", default="ready_to_apply", choices=["saved", "ready_to_apply", "applied", "interview", "rejected", "offer", "closed"])
    q.add_argument("--notes", default="")
    q.set_defaults(func=cmd_queue)

    ho = sub.add_parser("handoff", help="Prepare a self-contained Claude browser-operator packet for one job")
    ho.add_argument("job_id")
    ho.add_argument("--force", action="store_true", help="Rebuild even if nothing material changed since the last handoff")
    ho.add_argument("--authorize-submission", action="store_true", help="Set submission_authorized=true: Claude submits once every pre-submit check passes")
    ho.set_defaults(func=cmd_handoff)

    hb = sub.add_parser("handoff-batch", help="Prepare the next N eligible queued jobs for Claude independently; one failure never blocks the rest")
    hb.add_argument("--limit", type=int, default=10)
    hb.add_argument("--authorize-submission", action="store_true", help="Set submission_authorized=true for every packet in this batch")
    hb.set_defaults(func=cmd_handoff_batch)

    nx = sub.add_parser("next", help="Select the highest-priority eligible job not yet handed off to Claude")
    nx.add_argument("--include-needs-input", action="store_true", help="Also consider jobs currently stuck at CLAUDE_NEEDS_INPUT")
    nx.set_defaults(func=cmd_next)

    op = sub.add_parser("operate", help="Prepare the handoff for one job and print a ready-to-use Claude operator instruction")
    op.add_argument("job_id")
    op.add_argument("--force", action="store_true")
    op.add_argument("--authorize-submission", action="store_true", help="Set submission_authorized=true: Claude submits once every pre-submit check passes")
    op.set_defaults(func=cmd_operate)

    opn = sub.add_parser("operate-next", help="Select the next eligible job and print its Claude operator instruction")
    opn.add_argument("--include-needs-input", action="store_true")
    opn.add_argument("--force", action="store_true")
    opn.add_argument("--authorize-submission", action="store_true", help="Set submission_authorized=true: Claude submits once every pre-submit check passes")
    opn.set_defaults(func=cmd_operate_next)

    ir = sub.add_parser("import-result", help="Idempotently ingest output/<job_id>/claude_result.json into persistent application state")
    ir.add_argument("job_id")
    ir.set_defaults(func=cmd_import_result)

    ap = sub.add_parser("apply", help="Prepare or fill one official employer application safely")
    ap.add_argument("job_id")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Prepare artifacts only (default)")
    mode.add_argument("--submit", action="store_true", help="Request submission; still stops at unverified/unsupported steps")
    browser = ap.add_mutually_exclusive_group()
    browser.add_argument("--headed", action="store_true", default=True)
    browser.add_argument("--headless", action="store_true")
    ap.add_argument("--force", action="store_true", help="Explicitly recover a prior application state")
    ap.add_argument("--continue", dest="continue_run", action="store_true", help="Resume a prior safe run")
    ap.set_defaults(func=cmd_apply, dry_run=False)

    bs = sub.add_parser("browser-session", help="Launch or signal the one controlled headed browser session")
    bs.add_argument("job_id")
    bs_group = bs.add_mutually_exclusive_group()
    bs_group.add_argument("--continue", dest="action", action="store_const", const="continue", help="Signal that manual verification is complete")
    bs_group.add_argument("--close", dest="action", action="store_const", const="close", help="Close the controlled browser session")
    bs.set_defaults(func=cmd_browser_session, action=None)

    h = sub.add_parser("hydrate", help="Fetch and store the official job description")
    h.add_argument("job_id")
    h.add_argument("--force", action="store_true")
    h.set_defaults(func=cmd_hydrate)

    bp = sub.add_parser("batch-prepare", help="Hydrate, tailor, validate, and queue many jobs")
    bp.add_argument("--limit", type=int, default=25)
    bp.add_argument("--min-score", type=float)
    bp.add_argument("--no-hydrate", action="store_true")
    bp.add_argument("--force", action="store_true")
    bp.set_defaults(func=cmd_batch_prepare)

    ba = sub.add_parser("batch-apply", help="Run guarded applications serially with quota and duplicate protection")
    ba.add_argument("--limit", type=int)
    ba.add_argument("--min-score", type=float)
    ba.add_argument("--daily-limit", type=int)
    ba.add_argument("--submit", action="store_true")
    browser_batch = ba.add_mutually_exclusive_group()
    browser_batch.add_argument("--headed", action="store_true", default=False)
    browser_batch.add_argument("--headless", action="store_true")
    ba.set_defaults(func=cmd_batch_apply)

    dash = sub.add_parser("dashboard", help="Write a local HTML application dashboard")
    dash.add_argument("--output", default="output/dashboard.html")
    dash.set_defaults(func=cmd_dashboard)

    sub.add_parser("export-csv").set_defaults(func=cmd_export)
    sub.add_parser("export-mads-profile").set_defaults(func=cmd_export_mads)

    d = sub.add_parser("parse-jd", help="Parse a saved complete job-description JSON")
    d.add_argument("job_file")
    d.add_argument("--output")
    d.set_defaults(func=cmd_parse_jd)

    t = sub.add_parser("tailor", help="Build a truthful, auditable tailored resume from a saved job JSON")
    t.add_argument("job_file")
    t.add_argument("--output-dir", required=True)
    t.add_argument("--tex-name", default="resume.tex")
    t.add_argument("--preset", choices=["auto"], default="auto")
    t.add_argument("--dry-run", action="store_true", help="Parse and match requirements without writing or compiling a resume")
    t.set_defaults(func=cmd_tailor)

    c = sub.add_parser("coverage", help="Regenerate category-aware JD/evidence coverage without changing a resume")
    c.add_argument("job_file")
    c.add_argument("--output-dir", required=True)
    c.add_argument("--preset", choices=["auto"], default="auto")
    c.set_defaults(func=cmd_coverage)

    sub.add_parser("validate-evidence", help="Validate evidence IDs and required fields").set_defaults(func=cmd_validate_evidence)
    return p


def main():
    args = parser().parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
