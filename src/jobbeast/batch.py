"""Batch discovery, hydration, resume preparation, and guarded application runs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .apply.engine import run_apply
from .apply.state import ApplicationState
from .eligibility import classify_hard_eligibility
from .hydration import fetch_official
from .job_quality import assess_job
from .resume_builder import build_application
from .scorer import score_job
from .store import (get_job, has_verified_submission, list_jobs,
                    save_score, submitted_today, transition_application,
                    update_job_hydration, update_job_eligibility, ensure_application)
from .profile import forbidden_resume_terms, load_profile, required_resume_terms
from .validation import render_preview, validate_pdf


@dataclass
class BatchItem:
    job_id: str
    company: str
    title: str
    state: str
    action: str
    reason: str = ""
    output: str = ""

    def as_dict(self):
        return asdict(self)


def _job_dict(row) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys()}
    try:
        result["location"] = json.loads(result.get("locations_json") or "[]")[0]
    except (IndexError, TypeError, ValueError):
        result["location"] = ""
    return result


def hydrate_job(job_id: str, *, force: bool = False) -> dict:
    row = get_job(job_id)
    if not row:
        raise ValueError(f"Unknown job id: {job_id}")
    if row["description"] and not force and row["hydration_status"] == "HYDRATED":
        return {"job_id": job_id, "status": "already_hydrated", "description_length": len(row["description"] or "")}
    payload = fetch_official(row["url"])
    payload["jd_hash"] = hashlib.sha256(payload.get("description", "").encode()).hexdigest()
    update_job_hydration(job_id, payload)
    return {"job_id": job_id, "status": payload["status"], "description_length": len(payload.get("description", "")), "provider": payload.get("ats_provider")}


def prepare_batch(*, limit: int = 25, min_score: float | None = None, hydrate: bool = True, force: bool = False, root: Path | None = None) -> list[BatchItem]:
    root = root or Path(__file__).resolve().parents[2]
    items: list[BatchItem] = []
    for initial in list_jobs(limit=limit, min_score=min_score):
        row = initial
        job_id = row["id"]
        ensure_application(job_id)
        try:
            if hydrate and (not row["description"] or force or row["hydration_status"] != "HYDRATED"):
                hydrate_job(job_id, force=force)
            row = get_job(job_id)
            eligibility = classify_hard_eligibility(_job_dict(row), load_profile())
            update_job_eligibility(job_id, eligibility)
            if eligibility["status"] == "INELIGIBLE":
                transition_application(job_id, ApplicationState.INELIGIBLE_HARD_REQUIREMENT.value, event_type="hard_eligibility_failed", payload={"reason": eligibility["reason"], "hard_requirements": eligibility["hard_requirements"]}, force=force)
                items.append(BatchItem(job_id, row["company"], row["title"], "INELIGIBLE_HARD_REQUIREMENT", "skipped", eligibility["reason"]))
                continue
            quality = assess_job(_job_dict(row))
            if quality["suspicious"]:
                items.append(BatchItem(job_id, row["company"], row["title"], "NEEDS_HUMAN", "skipped", "; ".join(quality["warnings"])))
                continue
            report = score_job(row)
            save_score(job_id, report["score"], report)
            output_dir = root / "output" / job_id
            built = build_application(job=_job_dict(row), output_dir=output_dir)
            if not built.get("compiled"):
                raise RuntimeError(built.get("compile_log", "resume compilation failed")[-1200:])
            pdf = Path(built["pdf"])
            validation = validate_pdf(pdf, required_terms=required_resume_terms(), forbidden_terms=forbidden_resume_terms())
            (output_dir / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
            if validation.get("errors"):
                raise RuntimeError("; ".join(validation["errors"]))
            render_preview(pdf, output_dir / "resume_page1.png")
            try:
                transition_application(job_id, ApplicationState.RESUME_READY.value, event_type="batch_resume_ready", payload={"score": report["score"]}, force=force)
            except ValueError:
                # A previously advanced application remains authoritative.
                pass
            items.append(BatchItem(job_id, row["company"], row["title"], "RESUME_READY", "prepared", output=str(output_dir)))
        except Exception as exc:
            items.append(BatchItem(job_id, row["company"], row["title"], "FAILED", "failed", str(exc)))
    return items


def apply_batch(*, limit: int = 25, min_score: float | None = None, submit: bool = False, headed: bool = False, daily_limit: int = 75, root: Path | None = None) -> list[dict]:
    root = root or Path(__file__).resolve().parents[2]
    if submit and submitted_today() >= daily_limit:
        return [{"state": "NEEDS_HUMAN", "reason": f"daily limit of {daily_limit} reached"}]
    # `batch-apply` is intentionally end-to-end: hydrate and build artifacts
    # before opening a browser, even when the user did not run batch-prepare.
    prepared = prepare_batch(limit=limit, min_score=min_score, hydrate=True, root=root)
    allowed = {item.job_id for item in prepared if item.state == "RESUME_READY"}
    rows = list_jobs(limit=limit, min_score=min_score)
    results: list[dict] = []
    remaining = max(0, daily_limit - submitted_today()) if submit else limit
    for row in rows[:remaining]:
        if row["id"] not in allowed:
            results.append({"job_id": row["id"], "state": "FAILED", "reason": "batch preparation failed or was skipped"})
            continue
        if submit and has_verified_submission(row["id"]):
            results.append({"job_id": row["id"], "state": "NEEDS_HUMAN", "reason": "duplicate submission blocked"})
            continue
        try:
            results.append(run_apply(job_id=row["id"], submit=submit, headed=headed, root=root))
        except Exception as exc:
            results.append({"job_id": row["id"], "state": "FAILED", "error": str(exc)})
        if submit and submitted_today() >= daily_limit:
            break
    return results
