from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import urlparse

from .models import Job
from .apply.state import ApplicationState, can_transition, normalize_state


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    locations_json TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    date_posted TEXT,
    retrieved_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    terms_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    fit_score REAL,
    score_json TEXT,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(fit_score DESC);

CREATE TABLE IF NOT EXISTS source_listings (
    source TEXT NOT NULL,
    source_listing_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    source_date_posted TEXT,
    source_date_updated TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(source, source_listing_id),
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_source_listings_job ON source_listings(job_id);

CREATE TABLE IF NOT EXISTS source_syncs (
    source TEXT PRIMARY KEY,
    last_successful_sync TEXT,
    last_source_revision TEXT,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    closed_count INTEGER NOT NULL DEFAULT 0,
    received_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS application_queue (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT 'BACKLOG',
    queue_priority REAL NOT NULL DEFAULT 0,
    fit_score REAL,
    freshness_score REAL,
    eligibility_status TEXT,
    first_seen_at TEXT,
    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_application_queue_order ON application_queue(status, lane, queue_priority DESC, first_seen_at ASC);

CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'saved',
    resume_variant TEXT,
    applied_at TEXT,
    outcome TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    provider_job_id TEXT,
    company TEXT NOT NULL,
    provider TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    terminal_state TEXT,
    failure_class TEXT,
    artifacts_dir TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_application_runs_job ON application_runs(job_id, started_at);
CREATE INDEX IF NOT EXISTS idx_application_events_job ON application_events(job_id, created_at);
CREATE TABLE IF NOT EXISTS application_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 0,
    answer_source TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'unresolved',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
"""


def db_path() -> Path:
    return Path(os.getenv("JOBBEAST_DB", "data/jobs.db"))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The radar and the application worker share this DB; wait for a
    # concurrent writer (e.g. a full Simplify sync) instead of failing fast.
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    migrations = {
        "jobs": {
            "retrieved_at": "TEXT",
            "official_url": "TEXT", "canonical_url": "TEXT", "requisition_id": "TEXT",
            "ats_provider": "TEXT", "official_host_verified": "INTEGER DEFAULT 0",
            "ats_provider_confidence": "REAL", "ats_provider_evidence": "TEXT",
            "ats_provider_host": "TEXT", "provider_job_id": "TEXT", "fingerprint_json": "TEXT",
            "jd_fetched_at": "TEXT", "jd_hash": "TEXT", "eligibility_status": "TEXT",
            "eligibility_json": "TEXT", "hydration_status": "TEXT",
            "source_listing_id": "TEXT", "source_date_posted": "TEXT", "source_date_updated": "TEXT",
            "official_posted_at": "TEXT", "freshness_confidence": "TEXT",
        },
        "applications": {
            "state": "TEXT DEFAULT 'DISCOVERED'", "ats_provider": "TEXT",
            "submission_method": "TEXT",
            "canonical_application_url": "TEXT", "requisition_id": "TEXT",
            "resume_final_path": "TEXT", "resume_manifest_path": "TEXT",
            "answers_path": "TEXT", "preflight_path": "TEXT",
            "submission_receipt_path": "TEXT", "confirmation_screenshot_path": "TEXT",
            "last_url": "TEXT", "last_step": "TEXT", "last_error": "TEXT",
            "needs_human_reason": "TEXT", "submitted_at": "TEXT",
            "handoff_version": "INTEGER DEFAULT 0", "handoff_created_at": "TEXT",
            "handoff_source_hash": "TEXT", "handoff_result_hash": "TEXT",
        },
    }
    for table, additions in migrations.items():
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions.items():
            if name not in columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    con.execute("UPDATE applications SET state=CASE status WHEN 'applied' THEN 'SUBMITTED' WHEN 'ready_to_apply' THEN 'READY_TO_APPLY' WHEN 'saved' THEN 'DISCOVERED' ELSE UPPER(status) END WHERE state IS NULL")
    return con


def _job_source_hash(job: Job) -> str:
    import hashlib
    payload = {
        "company": job.company, "title": job.title, "url": job.url,
        "locations": job.locations, "terms": job.terms, "active": job.active,
        "description": job.description, "metadata": job.metadata,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# Hosts that list jobs on behalf of employers; their URL is never preferred
# over a direct employer/ATS URL for the same job.
AGGREGATOR_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "google.com")
_COMPANY_SUFFIX = re.compile(r"\b(?:the|inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|lp|usa|us|america|group|holdings)\b")
_TITLE_NOISE = re.compile(r"\b(?:summer|fall|spring|winter)\b|\b20\d\d\b")


def is_aggregator_url(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in AGGREGATOR_HOSTS)


def canonical_job_key(company: str, title: str) -> str:
    """Company + role key used to merge one job seen through different sources
    (e.g. "Hewlett Packard Enterprise Inc." / "Software Engineer Intern - Summer 2027")."""
    norm = lambda v: re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (v or "").lower())).strip()
    company_key = re.sub(r"\s+", " ", _COMPANY_SUFFIX.sub(" ", norm(company))).strip()
    title_key = re.sub(r"\s+", " ", _TITLE_NOISE.sub(" ", norm(title))).strip()
    return f"{company_key}|{title_key}" if company_key and title_key else ""


def sync_source_jobs(jobs: Iterable[Job], source: str, *, close_missing: bool = True) -> dict:
    """Persist a source delta while preserving cross-source deduplication.

    `close_missing` marks listings absent from this snapshot as closed; pass
    False for incremental sources (e.g. a "posted in the last hour" search)
    whose results are a window, not the full set of open listings.
    """
    jobs = list(jobs)
    now_ids = {str(job.source_id) for job in jobs if job.source_id}
    new_ids: list[str] = []
    updated_ids: list[str] = []
    closed_count = 0
    other_sources: dict[str, str] | None = None  # built lazily, once per sync
    with connect() as con:
        prior = {r["source_listing_id"]: r for r in con.execute("SELECT * FROM source_listings WHERE source=?", (source,))}
        for job in jobs:
            listing_id = str(job.source_id or job.metadata.get("source_listing_id") or "")
            if not listing_id:
                continue
            source_date_posted = job.posted_iso()
            raw_updated = job.metadata.get("date_updated")
            source_date_updated = None
            if isinstance(raw_updated, (int, float)) and raw_updated > 0:
                from datetime import datetime, timezone
                source_date_updated = datetime.fromtimestamp(raw_updated, timezone.utc).isoformat()
            payload_hash = _job_source_hash(job)
            prior_row = prior.get(listing_id)
            job_row = con.execute("SELECT id FROM jobs WHERE fingerprint=? OR url=? ORDER BY first_seen LIMIT 1", (job.fingerprint, job.url)).fetchone()
            if not prior_row and not job_row:
                # Same company + role already known through another source:
                # one canonical job, with this listing recorded as provenance.
                key = canonical_job_key(job.company, job.title)
                if other_sources is None:
                    other_sources = {}
                    for r in con.execute("SELECT id, company, title FROM jobs WHERE active=1 AND source<>? ORDER BY first_seen DESC", (source,)):
                        k = canonical_job_key(r["company"], r["title"])
                        if k:
                            other_sources[k] = r["id"]
                if key in other_sources:
                    job_row = {"id": other_sources[key]}
            job_id = prior_row["job_id"] if prior_row else (job_row["id"] if job_row else job.id)
            if job_row and job_row["id"] != job_id:
                job_id = job_row["id"]
            existing = con.execute("SELECT source, url, metadata_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            exists = existing is not None
            existing_meta = json.loads(existing["metadata_json"] or "{}") if exists else {}
            sources = sorted(set((existing_meta.get("sources") or []) + ([existing["source"]] if exists else []) + (job.metadata.get("sources") or []) + [source]))
            metadata = {**job.metadata, "sources": sources}
            owns = exists and existing["source"] == source
            # A direct employer URL always replaces an aggregator page (e.g. Simplify's
            # Greenhouse link over a LinkedIn listing), never the reverse.
            upgrades_url = exists and is_aggregator_url(existing["url"]) and job.url and not is_aggregator_url(job.url)
            if exists and not owns and not upgrades_url:
                con.execute("UPDATE jobs SET metadata_json=?, last_seen=CURRENT_TIMESTAMP WHERE id=?",
                            (json.dumps({**existing_meta, "sources": sources}), job_id))
            elif upgrades_url:
                con.execute(
                    """UPDATE jobs SET source=?, source_id=?, source_listing_id=?, source_date_posted=COALESCE(?, source_date_posted),
                       url=?, official_url=?, canonical_url=?, hydration_status=NULL, metadata_json=?, retrieved_at=?, last_seen=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (source, listing_id, listing_id, source_date_posted, job.url, job.url, job.url,
                     json.dumps({**existing_meta, **job.metadata, "sources": sources, "replaced_url": existing["url"]}), job.retrieved_iso(), job_id),
                )
            elif exists:
                con.execute(
                    """UPDATE jobs SET source_listing_id=?, source_date_posted=?, source_date_updated=?, company=?, title=?, url=?,
                       locations_json=?, terms_json=?, metadata_json=?, active=?, retrieved_at=?, last_seen=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (listing_id, source_date_posted, source_date_updated, job.company, job.title, job.url,
                     json.dumps(job.locations), json.dumps(job.terms), json.dumps(metadata), int(job.active), job.retrieved_iso(), job_id),
                )
            else:
                con.execute(
                    """INSERT INTO jobs (id,fingerprint,source,source_id,source_listing_id,source_date_posted,source_date_updated,company,title,url,locations_json,description,date_posted,retrieved_at,active,terms_json,metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, job.fingerprint, source, listing_id, listing_id, source_date_posted, source_date_updated, job.company, job.title, job.url,
                     json.dumps(job.locations), job.description, source_date_posted, job.retrieved_iso(), int(job.active), json.dumps(job.terms), json.dumps(metadata)),
                )
            if not prior_row:
                new_ids.append(job_id)
            elif prior_row["payload_hash"] != payload_hash:
                updated_ids.append(job_id)
            con.execute(
                """INSERT INTO source_listings(source,source_listing_id,job_id,payload_hash,source_date_posted,source_date_updated,active,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(source,source_listing_id) DO UPDATE SET job_id=excluded.job_id,payload_hash=excluded.payload_hash,
                   source_date_posted=excluded.source_date_posted,source_date_updated=excluded.source_date_updated,
                   active=excluded.active,last_seen_at=CURRENT_TIMESTAMP""",
                (source, listing_id, job_id, payload_hash, source_date_posted, source_date_updated, int(job.active)),
            )
        for listing_id, prior_row in (prior.items() if close_missing else ()):
            if listing_id not in now_ids and prior_row["active"]:
                con.execute("UPDATE source_listings SET active=0,last_seen_at=CURRENT_TIMESTAMP WHERE source=? AND source_listing_id=?", (source, listing_id))
                con.execute("UPDATE jobs SET active=0 WHERE id=? AND source=?", (prior_row["job_id"], source))
                closed_count += 1
        import hashlib
        revision = hashlib.sha256("|".join(sorted(f"{j.source_id}:{_job_source_hash(j)}" for j in jobs)).encode()).hexdigest()
        con.execute(
            """INSERT INTO source_syncs(source,last_successful_sync,last_source_revision,new_count,updated_count,closed_count,received_count)
               VALUES(?,CURRENT_TIMESTAMP,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET last_successful_sync=excluded.last_successful_sync,last_source_revision=excluded.last_source_revision,
               new_count=excluded.new_count,updated_count=excluded.updated_count,closed_count=excluded.closed_count,received_count=excluded.received_count""",
            (source, revision, len(new_ids), len(updated_ids), closed_count, len(jobs)),
        )
    return {"received": len(jobs), "new": new_ids, "updated": updated_ids, "closed": closed_count, "revision": revision}


def get_source_sync(source: str):
    with connect() as con:
        row = con.execute("SELECT * FROM source_syncs WHERE source=?", (source,)).fetchone()
        return dict(row) if row else None


def upsert_application_queue(job_id: str, *, status: str, lane: str, queue_priority: float, fit_score: float | None, freshness_score: float | None, eligibility_status: str, first_seen_at: str | None = None, last_error: str | None = None) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO application_queue(job_id,status,lane,queue_priority,fit_score,freshness_score,eligibility_status,first_seen_at,last_error)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,lane=excluded.lane,
               queue_priority=excluded.queue_priority,fit_score=excluded.fit_score,freshness_score=excluded.freshness_score,
               eligibility_status=excluded.eligibility_status,last_error=excluded.last_error""",
            (job_id, status, lane, queue_priority, fit_score, freshness_score, eligibility_status, first_seen_at, last_error),
        )


def queue_row(job_id: str):
    with connect() as con:
        return con.execute("SELECT * FROM application_queue WHERE job_id=?", (job_id,)).fetchone()


def park_queue(job_id: str, status: str, reason: str | None = None) -> None:
    """Record where a job ended up so it is never silently re-selected."""
    with connect() as con:
        cur = con.execute("UPDATE application_queue SET status=?, last_error=? WHERE job_id=?", (status, reason, job_id))
        if not cur.rowcount:
            con.execute("INSERT INTO application_queue(job_id,status,lane,queue_priority,eligibility_status,last_error) VALUES(?,?,?,?,?,?)",
                        (job_id, status, "BACKLOG", 0, "PENDING", reason))


def queued_jobs(limit: int = 10):
    with connect() as con:
        return con.execute(
            """SELECT q.*,j.company,j.title,j.url,j.locations_json,j.description,j.official_url,j.canonical_url
               FROM application_queue q JOIN jobs j ON j.id=q.job_id
               WHERE q.status IN ('QUEUED','FAST_LANE','RANKED') AND q.eligibility_status='ELIGIBLE'
               ORDER BY q.queue_priority DESC,q.first_seen_at ASC LIMIT ?""", (limit,)
        ).fetchall()


def upsert_jobs(jobs: Iterable[Job]) -> tuple[int, int]:
    inserted = updated = 0
    with connect() as con:
        for job in jobs:
            # Prefer an existing semantic duplicate even if it came from another source.
            existing = con.execute(
                "SELECT id FROM jobs WHERE fingerprint=? ORDER BY first_seen LIMIT 1",
                (job.fingerprint,),
            ).fetchone()
            if existing:
                con.execute(
                    """UPDATE jobs SET source=?, source_id=?, url=?, locations_json=?,
                       description=CASE WHEN length(?) > length(description) THEN ? ELSE description END,
                    date_posted=COALESCE(?, date_posted), retrieved_at=?, active=?, terms_json=?, metadata_json=?,
                       last_seen=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        job.source, job.source_id, job.url, json.dumps(job.locations),
                        job.description, job.description, job.posted_iso(), job.retrieved_iso(), int(job.active),
                        json.dumps(job.terms), json.dumps(job.metadata), existing["id"],
                    ),
                )
                updated += 1
            else:
                con.execute(
                    """INSERT INTO jobs
                    (id,fingerprint,source,source_id,company,title,url,locations_json,description,date_posted,retrieved_at,active,terms_json,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job.id, job.fingerprint, job.source, job.source_id, job.company,
                        job.title, job.url, json.dumps(job.locations), job.description,
                        job.posted_iso(), job.retrieved_iso(), int(job.active), json.dumps(job.terms), json.dumps(job.metadata),
                    ),
                )
                inserted += 1
    return inserted, updated


def get_job(job_id: str):
    with connect() as con:
        return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(limit: int = 50, min_score: float | None = None):
    with connect() as con:
        if min_score is None:
            return con.execute(
                "SELECT * FROM jobs WHERE active=1 ORDER BY COALESCE(fit_score,-1) DESC, date_posted DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return con.execute(
            "SELECT * FROM jobs WHERE active=1 AND fit_score>=? ORDER BY fit_score DESC, date_posted DESC LIMIT ?",
            (min_score, limit),
        ).fetchall()


def all_active_jobs():
    with connect() as con:
        return con.execute("SELECT * FROM jobs WHERE active=1").fetchall()


def update_job_hydration(job_id: str, payload: dict) -> None:
    """Persist official-page retrieval metadata without overwriting good data."""
    with connect() as con:
        con.execute(
            """UPDATE jobs SET description=CASE WHEN length(?) > length(description) THEN ? ELSE description END,
               official_url=?, canonical_url=?, ats_provider=?, official_host_verified=?,
               ats_provider_confidence=?, ats_provider_evidence=?, ats_provider_host=?, provider_job_id=?, fingerprint_json=?, jd_fetched_at=?,
               jd_hash=?, hydration_status=?, eligibility_status=?, eligibility_json=? WHERE id=?""",
            (
                payload.get("description", ""), payload.get("description", ""), payload.get("official_url"),
                payload.get("canonical_url"), payload.get("ats_provider"), int(bool(payload.get("official_host_verified"))),
                payload.get("ats_provider_confidence"), json.dumps(payload.get("ats_provider_evidence", [])),
                payload.get("ats_provider_host"), payload.get("provider_job_id"), json.dumps(payload.get("fingerprint", {})),
                payload.get("retrieved_at"), payload.get("jd_hash"), payload.get("status"),
                payload.get("eligibility_status"), json.dumps(payload.get("eligibility", {})), job_id,
            ),
            )


def update_job_detection(job_id: str, detection: dict) -> None:
    """Persist the complete ATS fingerprint observed at the official page."""
    with connect() as con:
        con.execute(
            """UPDATE jobs SET ats_provider=?, ats_provider_confidence=?, ats_provider_evidence=?,
               ats_provider_host=?, provider_job_id=?, fingerprint_json=?, canonical_url=?, official_host_verified=? WHERE id=?""",
            (
                detection.get("provider", "unknown"), detection.get("confidence", 0.0),
                json.dumps(detection.get("signals", [])), detection.get("provider_host", ""),
                detection.get("provider_job_id", ""), json.dumps(detection), detection.get("canonical_url"),
                int(bool(detection.get("official_host_verified"))), job_id,
            ),
        )


def update_job_eligibility(job_id: str, decision: dict) -> None:
    with connect() as con:
        con.execute(
            "UPDATE jobs SET eligibility_status=?, eligibility_json=? WHERE id=?",
            (decision.get("status"), json.dumps(decision), job_id),
        )


def update_queue_eligibility(job_id: str, status: str) -> None:
    """Correct a queued row's eligibility verdict when a recheck (e.g. at
    handoff time, against newly-hydrated JD text) disagrees with the
    original classification, so the row stops being surfaced as ELIGIBLE."""
    with connect() as con:
        con.execute(
            "UPDATE application_queue SET eligibility_status=?, status=CASE WHEN ? IN ('INELIGIBLE','NEEDS_CONFIRMATION') THEN ? ELSE status END WHERE job_id=?",
            (status, status, status, job_id),
        )


def save_score(job_id: str, score: float, report: dict):
    with connect() as con:
        con.execute(
            "UPDATE jobs SET fit_score=?, score_json=? WHERE id=?",
            (score, json.dumps(report), job_id),
        )


def submitted_today() -> int:
    with connect() as con:
        return int(con.execute("SELECT COUNT(*) FROM applications WHERE state='SUBMITTED' AND submitted_at >= date('now')").fetchone()[0])


def has_verified_submission(job_id: str) -> bool:
    with connect() as con:
        row = con.execute("SELECT fingerprint, company, title FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return False
        return bool(con.execute(
            """SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id
               WHERE a.state IN ('SUBMITTED','SUBMITTED_UNVERIFIED') AND (j.fingerprint=? OR (lower(j.company)=lower(?) AND lower(j.title)=lower(?))) LIMIT 1""",
            (row["fingerprint"], row["company"], row["title"]),
        ).fetchone())


def set_application(job_id: str, status: str, notes: str = ""):
    with connect() as con:
        con.execute(
            """INSERT INTO applications(job_id,status,state,notes) VALUES(?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, state=excluded.state, notes=excluded.notes, updated_at=CURRENT_TIMESTAMP""",
            (job_id, status, normalize_state(status), notes),
        )


def get_application(job_id: str):
    with connect() as con:
        return con.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()


def ensure_application(job_id: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO applications(job_id,status,state) VALUES(?,?,?) ON CONFLICT(job_id) DO NOTHING",
            (job_id, "saved", ApplicationState.DISCOVERED.value),
        )


def transition_application(job_id: str, target: str, *, event_type: str = "transition", payload: dict | None = None, force: bool = False) -> str:
    ensure_application(job_id)
    target = normalize_state(target)
    with connect() as con:
        row = con.execute("SELECT state FROM applications WHERE job_id=?", (job_id,)).fetchone()
        current = normalize_state(row["state"] if row and row["state"] else "DISCOVERED")
        manual_submission = (
            target == ApplicationState.SUBMITTED.value
            and event_type == "manual_submission_recorded"
            and (payload or {}).get("method") == "MANUAL"
            and bool((payload or {}).get("official_confirmation"))
        )
        if not can_transition(current, target, force=force) and not manual_submission:
            raise ValueError(f"Invalid application transition {current} -> {target}")
        legacy = {
            "SUBMITTED": "applied", "READY_TO_APPLY": "ready_to_apply", "APPLYING": "applying",
            "NEEDS_INPUT": "needs_input", "NEEDS_HUMAN": "needs_human", "CLOSED": "closed",
            "INELIGIBLE_HARD_REQUIREMENT": "ineligible_hard_requirement",
            "READY_FOR_CLAUDE": "ready_for_claude", "CLAUDE_RUNNING": "claude_running",
            "CLAUDE_NEEDS_INPUT": "claude_needs_input", "CLAUDE_SUBMITTED": "claude_submitted",
            "CLAUDE_FAILED": "claude_failed",
        }.get(target, "saved")
        con.execute("UPDATE applications SET state=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (target, legacy, job_id))
        con.execute("INSERT INTO application_events(job_id,from_state,to_state,event_type,payload_json) VALUES(?,?,?,?,?)", (job_id, current, target, event_type, json.dumps(payload or {})))
    return target


def update_application(job_id: str, **fields) -> None:
    allowed = {"ats_provider", "submission_method", "canonical_application_url", "requisition_id", "resume_final_path", "resume_manifest_path", "answers_path", "preflight_path", "submission_receipt_path", "confirmation_screenshot_path", "last_url", "last_step", "last_error", "needs_human_reason", "submitted_at", "notes", "handoff_version", "handoff_created_at", "handoff_source_hash", "handoff_result_hash"}
    clean = {key: value for key, value in fields.items() if key in allowed}
    if not clean:
        return
    assignments = ", ".join(f"{key}=?" for key in clean)
    with connect() as con:
        con.execute(f"UPDATE applications SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (*clean.values(), job_id))


def record_manual_submission(job_id: str, *, submitted_at: str, official_confirmation: str, notes: str = "") -> None:
    """Record a user-confirmed manual submission without fabricating automation artifacts."""
    ensure_application(job_id)
    payload = {
        "method": "MANUAL",
        "official_confirmation": official_confirmation,
        "receipt_created": False,
        "evidence_file_created": False,
    }
    transition_application(
        job_id,
        ApplicationState.SUBMITTED.value,
        event_type="manual_submission_recorded",
        payload=payload,
    )
    update_application(
        job_id,
        submission_method="MANUAL",
        submitted_at=submitted_at,
        last_step="manual_submission_recorded",
        needs_human_reason=None,
        notes=notes,
    )


def record_application_artifact(job_id: str, kind: str, path: str, sha256: str | None = None, metadata: dict | None = None) -> None:
    with connect() as con:
        con.execute("INSERT INTO application_artifacts(job_id,kind,path,sha256,metadata_json) VALUES(?,?,?,?,?)", (job_id, kind, path, sha256, json.dumps(metadata or {})))


def record_application_question(job_id: str, question: dict) -> None:
    with connect() as con:
        con.execute("INSERT INTO application_questions(job_id,question,category,required,answer_source,confidence,status) VALUES(?,?,?,?,?,?,?)", (job_id, question.get("question", ""), question.get("category", "UNKNOWN"), int(question.get("required", False)), question.get("answer_source"), question.get("confidence"), question.get("status", "unresolved")))


def application_rows():
    with connect() as con:
        return con.execute(
            """SELECT a.*, j.company, j.title, j.url, j.fit_score
               FROM applications a JOIN jobs j ON j.id=a.job_id
               ORDER BY a.updated_at DESC"""
        ).fetchall()
