"""Fresh internship radar and fault-tolerant application queue."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import hashlib
from typing import Callable

from .eligibility import classify_hard_eligibility
from .hydration import fetch_official
from .profile import load_profile
from .scorer import score_job
from .sources.simplify import fetch as fetch_simplify
from .store import (
    connect, get_job, get_source_sync, queued_jobs, save_score,
    sync_source_jobs, update_job_eligibility, update_job_hydration, upsert_application_queue,
)


# Freshness is a bounded bonus on top of fit, never a substitute for it: the
# largest bonus (15) is smaller than the fit gap between a weak and a strong
# match, and it is only granted at or above FRESHNESS_MIN_FIT.
FRESHNESS = ((1, "ULTRA_FRESH", 15), (3, "VERY_FRESH", 12), (6, "FRESH", 9),
             (12, "PRIORITY_FRESH", 6), (24, "RECENT", 3), (72, "NORMAL", 0))
FRESHNESS_MIN_FIT = 55.0
FAST_LANE_MIN_FIT = 70.0
# Application states the radar must never disturb: handed to Claude, being
# operated, parked for a human, or finished.
RADAR_FROZEN_STATES = {
    "READY_FOR_CLAUDE", "CLAUDE_RUNNING", "CLAUDE_NEEDS_INPUT", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_HUMAN_MFA",
    "CLAUDE_SUBMITTED", "SUBMITTED", "SUBMITTED_UNVERIFIED", "INELIGIBLE_HARD_REQUIREMENT", "CLOSED",
}


def queue_placement(fit: float | None, fresh: dict) -> dict:
    """Lane + priority from separate, auditable fit and freshness components."""
    fit = float(fit or 0.0)
    age = fresh.get("age_hours")
    bonus = float(fresh.get("bonus", 0)) if fit >= FRESHNESS_MIN_FIT else 0.0
    if age is not None and age <= 3 and fit >= FAST_LANE_MIN_FIT:
        lane = "FAST_LANE"
    elif age is not None and age <= 12 and fit >= FRESHNESS_MIN_FIT:
        lane = "PRIORITY"
    else:
        lane = "BACKLOG"
    return {"lane": lane, "status": "FAST_LANE" if lane == "FAST_LANE" else "QUEUED", "freshness_score": bonus, "queue_priority": round(fit + bonus, 2)}


def age_hours(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, ((now or datetime.now(timezone.utc)) - dt.astimezone(timezone.utc)).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


def freshness(value: str | None) -> dict:
    age = age_hours(value)
    if age is None:
        return {"age_hours": None, "bucket": "NORMAL", "bonus": 0, "confidence": "LOW"}
    for limit, bucket, bonus in FRESHNESS:
        if age <= limit:
            return {"age_hours": round(age, 2), "bucket": bucket, "bonus": bonus, "confidence": "MEDIUM"}
    return {"age_hours": round(age, 2), "bucket": "STALE", "bonus": 0, "confidence": "MEDIUM"}


def effective_freshness(row, *, at: datetime | None = None) -> dict:
    """Freshness from the best available evidence, never from a fabricated time.

    An exact source posting time is used as is. When the source only gave a
    date (or nothing) but the listing came from a posted-within-N-minutes
    search, the strongest fact is "posted at most N minutes before Job Beast
    first saw it", so freshness is measured from first_seen.
    """
    keys = row.keys()
    meta = json.loads(row["metadata_json"] or "{}") if "metadata_json" in keys else {}
    posted = (row["source_date_posted"] if "source_date_posted" in keys else None) or (row["date_posted"] if "date_posted" in keys else None)
    precision = meta.get("source_posted_precision")
    window = meta.get("source_window_minutes")
    first_seen = row["first_seen"] if "first_seen" in keys else None
    if precision in ("DATE_ONLY", "UNKNOWN") and window and first_seen:
        fresh = freshness(first_seen)
        since_seen = age_hours(first_seen, now=at)
        fresh.update({
            "basis": "FIRST_SEEN_WITHIN_SOURCE_WINDOW", "confidence": "MEDIUM",
            "source_posted_precision": precision, "source_window_minutes": window,
            # The employer's minute is unknown; this is the provable upper bound.
            "max_age_hours": round((since_seen or 0.0) + window / 60, 2),
        })
        return fresh
    fresh = freshness(posted)
    fresh.update({"basis": "SOURCE_POSTED" if posted else "NONE",
                  "max_age_hours": fresh["age_hours"]})
    if posted and precision == "EXACT":
        fresh["confidence"] = "HIGH"
    return fresh


def _row_dict(row) -> dict:
    result = {key: row[key] for key in row.keys()}
    result["location"] = (json.loads(result.get("locations_json") or "[]") or [""])[0]
    return result


OFF_TARGET = "OFF_TARGET_SOURCE_FILTER"


def _off_target(row) -> bool:
    """A LinkedIn-only job that is not a technical internship (the source filter's rule)."""
    if row["source"] != "linkedin":
        return False
    from .sources.apify_linkedin import is_target_role
    meta = json.loads(row["metadata_json"] or "{}")
    return not is_target_role({"title": row["title"], "employmentType": meta.get("employment_type"),
                               "seniorityLevel": meta.get("seniority_level"), "descriptionText": row["description"]})


def park_off_target_jobs() -> list[dict]:
    """Move LinkedIn-only, non-technical jobs out of active lanes (source history kept)."""
    with connect() as con:
        rows = con.execute("""SELECT j.*, q.status AS q_status FROM jobs j JOIN application_queue q ON q.job_id=j.id
                              WHERE j.source='linkedin' AND q.status<>'SKIPPED'""").fetchall()
    parked = []
    for row in rows:
        if _application_state(row["id"]) in RADAR_FROZEN_STATES or not _off_target(row):
            continue
        upsert_application_queue(row["id"], status="SKIPPED", lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None,
                                 eligibility_status=row["eligibility_status"] or "PENDING", first_seen_at=row["first_seen"], last_error=OFF_TARGET)
        parked.append({"job_id": row["id"], "company": row["company"], "title": row["title"], "previous_status": row["q_status"]})
    return parked


def _application_state(job_id: str) -> str | None:
    with connect() as con:
        row = con.execute("SELECT state FROM applications WHERE job_id=?", (job_id,)).fetchone()
    return row["state"] if row else None


def _process_job(job_id: str) -> dict:
    try:
        row = get_job(job_id)
        if not row:
            return {"job_id": job_id, "status": "FAILED", "reason": "job disappeared"}
        state = _application_state(job_id)
        if state in RADAR_FROZEN_STATES:
            # A radar refresh never re-queues, re-ranks, or re-classifies an
            # application that is with Claude, parked for a human, or finished.
            return {"job_id": job_id, "status": "UNCHANGED_ACTIVE_APPLICATION", "company": row["company"], "title": row["title"], "reason": state}
        if _off_target(row):
            upsert_application_queue(job_id, status="SKIPPED", lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None, eligibility_status=row["eligibility_status"] or "PENDING", first_seen_at=row["first_seen"], last_error=OFF_TARGET)
            return {"job_id": job_id, "status": "SKIPPED", "company": row["company"], "title": row["title"], "reason": OFF_TARGET}
        if not row["description"] or row["hydration_status"] != "HYDRATED":
            payload = fetch_official(row["url"])
            payload["jd_hash"] = hashlib.sha256(payload.get("description", "").encode()).hexdigest()
            update_job_hydration(job_id, payload)
        row = get_job(job_id)
        decision = classify_hard_eligibility(_row_dict(row), load_profile())
        update_job_eligibility(job_id, decision)
        if decision["status"] != "ELIGIBLE":
            status = "INELIGIBLE" if decision["status"] == "INELIGIBLE" else "NEEDS_CONFIRMATION"
            upsert_application_queue(job_id, status=status, lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None, eligibility_status=decision["status"], first_seen_at=row["first_seen"])
            return {"job_id": job_id, "status": status, "company": row["company"], "title": row["title"], "reason": decision.get("reason", "")}
        report = score_job(row)
        save_score(job_id, report["score"], report)
        fresh = effective_freshness(row)
        place = queue_placement(report["score"], fresh)
        upsert_application_queue(job_id, status=place["status"], lane=place["lane"], queue_priority=place["queue_priority"], fit_score=report["score"], freshness_score=place["freshness_score"], eligibility_status="ELIGIBLE", first_seen_at=row["first_seen"])
        return {"job_id": job_id, "status": place["status"], "company": row["company"], "title": row["title"], "fit_score": report["score"], "freshness": fresh, "priority": place["queue_priority"]}
    except Exception as exc:
        row = get_job(job_id)
        if row:
            upsert_application_queue(job_id, status="NEEDS_CONFIRMATION", lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None, eligibility_status="NEEDS_CONFIRMATION", first_seen_at=row["first_seen"], last_error=str(exc))
        return {"job_id": job_id, "status": "NEEDS_CONFIRMATION", "reason": str(exc)}


def _rank_delta(source: str, jobs: list, delta: dict, max_process: int) -> dict:
    fresh_ids = list(dict.fromkeys(delta["new"] + delta["updated"]))
    with connect() as con:
        pending = [r["job_id"] for r in con.execute("SELECT job_id FROM application_queue WHERE status='RANKED' AND eligibility_status='PENDING' ORDER BY first_seen_at ASC LIMIT ?", (max_process,))]
    # Process this sync's listings first (newest first), then older backlog, so
    # the backlog stays discoverable but can never starve a fresh listing --
    # including one whose source gave no posting time.
    fresh_ids.sort(key=lambda jid: (get_job(jid)["source_date_posted"] or ""), reverse=True)
    backlog = [jid for jid in pending if jid not in set(fresh_ids)]
    backlog.sort(key=lambda jid: (get_job(jid)["source_date_posted"] or ""), reverse=True)
    changed = fresh_ids + backlog
    processed = [_process_job(jid) for jid in changed[:max_process]]
    for jid in changed[max_process:]:
        row = get_job(jid)
        if row and _application_state(jid) not in RADAR_FROZEN_STATES:
            upsert_application_queue(jid, status="RANKED", lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None, eligibility_status="PENDING", first_seen_at=row["first_seen"], last_error="deferred by radar processing limit")
    fresh_counts = {"<=1h": 0, "<=3h": 0, "<=12h": 0}
    for job in jobs:
        value = job.posted_iso()
        age = age_hours(value)
        if age is not None:
            if age <= 1: fresh_counts["<=1h"] += 1
            if age <= 3: fresh_counts["<=3h"] += 1
            if age <= 12: fresh_counts["<=12h"] += 1
    reconcile_queue_with_applications()
    rechecked = recheck_queue_eligibility()
    rerank_queue()
    return {"source": source, "eligibility_rechecked_demoted": rechecked, "sync": {"received": delta["received"], "new": len(delta["new"]), "updated": len(delta["updated"]), "closed": delta["closed"], "revision": delta["revision"]}, "fresh_counts": fresh_counts, "processed": processed, "last_sync": get_source_sync(source)}


def sync_and_rank(*, max_process: int = 100, url: str | None = None, season: str = "Summer 2027") -> dict:
    jobs = fetch_simplify(url=url, season=season, active_only=False, visible_only=True) if url else fetch_simplify(season=season, active_only=False, visible_only=True)
    return _rank_delta("simplify", jobs, sync_source_jobs(jobs, "simplify"), max_process)


def _hydrate_from_listing(job_id: str, job) -> None:
    """Use the scraped LinkedIn description so ranking needs no extra fetch.

    LinkedIn pages are not the employer's official page, so the host stays
    unverified; the operator's live recheck confirms the posting before applying.
    """
    row = get_job(job_id)
    if not row or row["hydration_status"] == "HYDRATED" or len(job.description) < 300:
        return
    update_job_hydration(job_id, {
        "description": job.description,
        "official_url": job.url,
        "canonical_url": job.metadata.get("source_url") or job.url,
        "ats_provider": None,
        "official_host_verified": False,
        "ats_provider_confidence": "LOW",
        "ats_provider_evidence": [f"apify linkedin listing; application_route={job.metadata.get('application_route')}"],
        "retrieved_at": job.retrieved_iso(),
        "jd_hash": hashlib.sha256(job.description.encode()).hexdigest(),
        "status": "HYDRATED",
        "eligibility_status": row["eligibility_status"],
    })


def sync_linkedin_and_rank(*, max_process: int = 100, cfg: dict | None = None, fetcher: Callable | None = None) -> dict:
    """Fetch recent LinkedIn postings through Apify, then rank them like any other source."""
    from .sources import apify_linkedin
    cfg = cfg or {}
    fetcher = fetcher or apify_linkedin.fetch
    started = datetime.now(timezone.utc)
    fetched = fetcher(
        cfg.get("titles") or [],
        location=cfg.get("location", "United States"),
        window_seconds=int(cfg.get("window_minutes", 60)) * 60,
        limit_per_search=int(cfg.get("limit_per_search", 25)),
        actor=cfg.get("actor", apify_linkedin.DEFAULT_ACTOR),
        concurrency=int(cfg.get("concurrency", 3)),
    )
    jobs, stats = fetched["jobs"], dict(fetched["stats"])
    with connect() as con:
        existing = {r["id"]: r["source"] for r in con.execute("SELECT id, source FROM jobs")}
    # Each run only sees a posted-within window, so absent listings are not closed.
    delta = sync_source_jobs(jobs, apify_linkedin.SOURCE, close_missing=False)
    by_listing = {job.source_id: job for job in jobs}
    with connect() as con:
        rows = con.execute("SELECT source_listing_id, job_id FROM source_listings WHERE source=?", (apify_linkedin.SOURCE,)).fetchall()
    listing_job = {r["source_listing_id"]: r["job_id"] for r in rows if r["source_listing_id"] in by_listing}
    distinct = list(dict.fromkeys(listing_job.values()))
    new = [jid for jid in distinct if jid not in existing]
    for listing_id, job_id in listing_job.items():
        if job_id in new:
            _hydrate_from_listing(job_id, by_listing[listing_id])
    result = _rank_delta(apify_linkedin.SOURCE, jobs, delta, max_process)
    result["off_target_parked"] = park_off_target_jobs()
    result["fresh_counts"] = _window_fresh_counts(distinct, started)
    processed = {p["job_id"]: p for p in result["processed"]}
    stats.update({
        "distinct_jobs": len(distinct),
        "new": len(new),
        "updated": len([jid for jid in dict.fromkeys(delta["updated"]) if jid not in new]),
        # Same listing returned by several queries, plus reposts merged into one job.
        "duplicates": (stats["raw_results"] - stats.get("unique_listings", stats["raw_results"])) + (len(jobs) - len(distinct)),
        "cross_source_duplicates": len([jid for jid in distinct if existing.get(jid) not in (None, apify_linkedin.SOURCE)]),
        "ineligible": sum(processed.get(jid, {}).get("status") == "INELIGIBLE" for jid in distinct),
        "needs_confirmation": sum(processed.get(jid, {}).get("status") == "NEEDS_CONFIRMATION" for jid in distinct),
    })
    result["fetch"] = stats
    result["job_ids"] = distinct
    return result


def _window_fresh_counts(job_ids: list[str], at: datetime) -> dict:
    """Posted-within counts for this fetch, using each job's provable maximum age at fetch time."""
    counts = {"<=1h": 0, "<=3h": 0, "<=12h": 0}
    for jid in job_ids:
        row = get_job(jid)
        age = effective_freshness(row, at=at)["max_age_hours"] if row else None
        if age is None:
            continue
        if age <= 1: counts["<=1h"] += 1
        if age <= 3: counts["<=3h"] += 1
        if age <= 12: counts["<=12h"] += 1
    return counts


def reconcile_queue_with_applications() -> int:
    """Mirror frozen application states onto their queue rows, and park closed
    listings, so the queue never shows a submitted / handed-off / parked /
    closed job as waiting."""
    marks = ",".join("?" * len(RADAR_FROZEN_STATES))
    with connect() as con:
        cur = con.execute(
            f"""UPDATE application_queue SET status=(SELECT state FROM applications a WHERE a.job_id=application_queue.job_id)
                WHERE job_id IN (SELECT job_id FROM applications WHERE state IN ({marks}))
                AND status IS NOT (SELECT state FROM applications a WHERE a.job_id=application_queue.job_id)""",
            tuple(RADAR_FROZEN_STATES))
        # A listing its source has closed leaves the active lanes.
        closed = con.execute(f"""UPDATE application_queue SET status='CLOSED', last_error='SOURCE_LISTING_CLOSED'
                                 WHERE status IN ('QUEUED','FAST_LANE','RANKED')
                                 AND job_id IN (SELECT id FROM jobs WHERE active=0)
                                 AND job_id NOT IN (SELECT job_id FROM applications WHERE state IN ({marks}))""", tuple(RADAR_FROZEN_STATES))
        return cur.rowcount + closed.rowcount


def recheck_queue_eligibility() -> list[dict]:
    """Re-run the hard-eligibility rules over every waiting queue row.

    Cheap (stored text only, no network). New rules -- e.g. graduation windows
    -- therefore apply to jobs queued before the rule existed, before any
    resume or browser work is spent on them.
    """
    profile = load_profile()
    with connect() as con:
        rows = con.execute("""SELECT j.* FROM application_queue q JOIN jobs j ON j.id=q.job_id
                              WHERE q.status IN ('QUEUED','FAST_LANE','RANKED') AND q.eligibility_status='ELIGIBLE'""").fetchall()
    demoted = []
    for row in rows:
        if _application_state(row["id"]) in RADAR_FROZEN_STATES:
            continue
        decision = classify_hard_eligibility(_row_dict(row), profile)
        if decision["status"] == "ELIGIBLE":
            continue
        update_job_eligibility(row["id"], decision)
        status = "INELIGIBLE" if decision["status"] == "INELIGIBLE" else "NEEDS_CONFIRMATION"
        upsert_application_queue(row["id"], status=status, lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None,
                                 eligibility_status=decision["status"], first_seen_at=row["first_seen"], last_error=decision.get("reason"))
        demoted.append({"job_id": row["id"], "company": row["company"], "title": row["title"], "status": status, "reason": decision.get("reason")})
    return demoted


def rerank_queue() -> dict:
    """Recompute freshness, lane and priority for every waiting queue row.

    Lanes were previously fixed at queue time, so week-old rows kept their
    FAST_LANE/PRIORITY placement. Fit is not recomputed (no network); only the
    time-dependent parts change.
    """
    with connect() as con:
        rows = con.execute("""SELECT q.job_id,q.fit_score,j.source_date_posted,j.date_posted,j.first_seen,j.metadata_json
                              FROM application_queue q JOIN jobs j ON j.id=q.job_id
                              WHERE q.status IN ('QUEUED','FAST_LANE') AND q.eligibility_status='ELIGIBLE'""").fetchall()
        changed = 0
        for r in rows:
            place = queue_placement(r["fit_score"], effective_freshness(r))
            cur = con.execute("UPDATE application_queue SET status=?,lane=?,queue_priority=?,freshness_score=? WHERE job_id=? AND (status<>? OR lane<>? OR queue_priority<>? OR freshness_score IS NOT ?)",
                              (place["status"], place["lane"], place["queue_priority"], place["freshness_score"], r["job_id"],
                               place["status"], place["lane"], place["queue_priority"], place["freshness_score"]))
            changed += cur.rowcount
    return {"rows": len(rows), "changed": changed}


def fresh_report(limit: int = 10) -> dict:
    with connect() as con:
        rows = con.execute("SELECT q.*,j.company,j.title,j.url,j.source,j.source_date_posted,j.date_posted,j.first_seen,j.metadata_json FROM application_queue q JOIN jobs j ON j.id=q.job_id WHERE q.eligibility_status='ELIGIBLE' AND q.status IN ('FAST_LANE','QUEUED','RANKED') ORDER BY q.queue_priority DESC,q.first_seen_at ASC LIMIT ?", (limit,)).fetchall()
        counts = {"eligible": con.execute("SELECT COUNT(*) FROM application_queue WHERE eligibility_status='ELIGIBLE'").fetchone()[0], "ineligible": con.execute("SELECT COUNT(*) FROM application_queue WHERE status='INELIGIBLE'").fetchone()[0], "needs_confirmation": con.execute("SELECT COUNT(*) FROM application_queue WHERE status='NEEDS_CONFIRMATION'").fetchone()[0]}
    return {"last_sync": get_source_sync("simplify"), "counts": counts, "metrics": queue_latency_metrics(), "top": [{"job_id": r["job_id"], "company": r["company"], "title": r["title"], "source": r["source"], "age_hours": effective_freshness(r)["age_hours"], "age_basis": effective_freshness(r)["basis"], "fit_score": r["fit_score"], "priority": r["queue_priority"], "lane": r["lane"], "eligibility": r["eligibility_status"]} for r in rows]}


def queue_latency_metrics() -> dict:
    """Report queue latency by cohort; historical backlog is never the fresh KPI."""
    now = datetime.now(timezone.utc)
    with connect() as con:
        rows = con.execute("SELECT q.first_seen_at,q.queued_at,j.source_date_posted,j.date_posted FROM application_queue q JOIN jobs j ON j.id=q.job_id WHERE q.first_seen_at IS NOT NULL AND q.queued_at IS NOT NULL").fetchall()
    values = []
    fresh12 = []
    fresh24 = []
    for row in rows:
        try:
            first = datetime.fromisoformat(str(row["first_seen_at"]).replace("Z", "+00:00"))
            queued = datetime.fromisoformat(str(row["queued_at"]).replace("Z", "+00:00"))
            if not first.tzinfo: first = first.replace(tzinfo=timezone.utc)
            if not queued.tzinfo: queued = queued.replace(tzinfo=timezone.utc)
            minutes = max(0.0, (queued - first).total_seconds() / 60)
            values.append(minutes)
            posted = row["source_date_posted"] or row["date_posted"]
            age = age_hours(posted)
            if age is not None and age <= 12: fresh12.append(minutes)
            if age is not None and age <= 24: fresh24.append(minutes)
        except (TypeError, ValueError):
            continue

    def percentile(items, fraction):
        if not items: return None
        ordered = sorted(items); index = min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))
        return round(ordered[index], 2)

    return {
        "all_time": {"count": len(values), "median_minutes": percentile(values, 0.5)},
        "current_canary": {"count": 0, "median_minutes": None},
        "fresh_12h": {"count": len(fresh12), "median_discovery_to_queue_minutes": percentile(fresh12, 0.5), "p90_discovery_to_queue_minutes": percentile(fresh12, 0.9)},
        "fresh_24h": {"count": len(fresh24), "median_discovery_to_queue_minutes": percentile(fresh24, 0.5), "p90_discovery_to_queue_minutes": percentile(fresh24, 0.9)},
    }


def run_watch(*, interval_minutes: int = 30, once: bool = False, max_process: int = 100, callback: Callable[[dict], None] | None = None,
              sources: tuple[str, ...] = ("simplify",), linkedin_cfg: dict | None = None) -> None:
    runners = {"simplify": lambda: sync_and_rank(max_process=max_process),
               "linkedin": lambda: sync_linkedin_and_rank(max_process=max_process, cfg=linkedin_cfg)}
    while True:
        # One failing source never blocks the others.
        for name in sources:
            try:
                result = runners[name]()
                if callback:
                    callback(result)
            except Exception as exc:
                if callback:
                    callback({"source": name, "status": "TRANSIENT_FAILURE", "error": str(exc)})
        if once:
            return
        time.sleep(max(60, interval_minutes * 60))


def run_worker(*, limit: int = 1, submit: bool = False, headed: bool = False, root=None, runner=None) -> list[dict]:
    if runner is None:
        from .apply.engine import run_apply as runner
    results = []
    for row in queued_jobs(limit):
        jid = row["job_id"]
        upsert_application_queue(jid, status="PREPARING", lane=row["lane"], queue_priority=row["queue_priority"], fit_score=row["fit_score"], freshness_score=row["freshness_score"], eligibility_status=row["eligibility_status"], first_seen_at=row["first_seen_at"])
        try:
            result = runner(job_id=jid, submit=submit, headed=headed, root=root)
            results.append(result)
            state = result.get("state", "FAILED")
            queue_state = "SUBMITTED" if state == "SUBMITTED" else ("NEEDS_HUMAN" if state in {"NEEDS_INPUT", "NEEDS_HUMAN"} else "FAILED")
            upsert_application_queue(jid, status=queue_state, lane=row["lane"], queue_priority=row["queue_priority"], fit_score=row["fit_score"], freshness_score=row["freshness_score"], eligibility_status=row["eligibility_status"], first_seen_at=row["first_seen_at"])
        except Exception as exc:
            results.append({"job_id": jid, "state": "FAILED", "error": str(exc)})
            upsert_application_queue(jid, status="FAILED", lane=row["lane"], queue_priority=row["queue_priority"], fit_score=row["fit_score"], freshness_score=row["freshness_score"], eligibility_status=row["eligibility_status"], first_seen_at=row["first_seen_at"], last_error=str(exc))
    return results
