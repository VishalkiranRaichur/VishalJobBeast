"""Stop-safe single-application orchestration."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .answers import AnswerVault
from .artifacts import promote_resume, sha256_file
from .browser import BrowserUnavailable, close_browser, open_browser
from .bridge import BridgeRun, build_packet
from .detector import Detection, detect_provider
from .preflight import run_preflight
from .providers import PROVIDERS
from .receipts import write_receipt, write_unresolved
from .state import ApplicationState
from ..eligibility import classify_hard_eligibility
from ..store import (get_application, get_job, record_application_artifact,
                     has_verified_submission, record_application_question,
                     transition_application, update_application, update_job_eligibility)
from ..profile import PROFILE_DIR, forbidden_resume_terms, load_profile, required_resume_terms
from ..resume_builder import build_application
from ..validation import render_preview, validate_pdf


@dataclass
class ApplyResult:
    job_id: str
    state: str
    provider: str
    dry_run: bool
    preflight: dict[str, Any]
    artifacts: dict[str, str]
    details: dict[str, Any]

    def as_dict(self):
        return asdict(self)


def _job_dict(row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _run_extension(*, job: dict, output_dir: Path, resume_path: Path, answers: AnswerVault, submit: bool) -> tuple[str, dict]:
    """Run the normal-Chrome companion as the primary ATS executor."""
    bridge = BridgeRun(
        packet=build_packet(job=job, answers=answers, resume_path=resume_path, submit=submit, duplicate=False),
        output_dir=output_dir,
    )
    bridge.start()
    try:
        bridge.open_application()
        raw = bridge.wait()
        status = str(raw.get("status", "NEEDS_HUMAN"))
        details = {
            "detection": {"provider": job.get("ats_provider") or "unknown"},
            "result": {
                "status": status,
                "current_url": raw.get("current_url", job["url"]),
                "confirmation_text": raw.get("confirmation_text", ""),
                "details": raw,
                "screenshot": raw.get("screenshot"),
            },
            "questions": [
                {"question": item.get("label", ""), "required": True, "status": "unresolved"}
                for item in raw.get("unresolved_questions", [])
                if isinstance(item, dict)
            ],
        }
        return status, details
    finally:
        bridge.stop()


async def _run_browser(*, job: dict, output_dir: Path, provider_name: str, resume_path: Path, answers: AnswerVault, submit: bool, headed: bool) -> tuple[str, dict]:
    profile_dir = Path(job.get("_root", Path.cwd())) / "data" / "browser-profile"
    handle = None
    trace_started = False
    status = "NEEDS_HUMAN"
    diagnostics = output_dir / "browser"
    diagnostics.mkdir(parents=True, exist_ok=True)
    try:
        handle = await open_browser(profile_dir=profile_dir, headed=headed, reuse=True)
        context = handle.context
        page = handle.page
        try:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
            trace_started = True
        except Exception:
            pass
        if not handle.attached:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await page.screenshot(path=str(diagnostics / "navigation.png"), full_page=True)
        html = await page.content()
        detection = detect_provider(job["url"], html=html, final_url=page.url)
        page_text = (await page.locator("body").inner_text()).lower()
        title = (await page.title()).lower()
        challenge = any(token in f"{title} {page_text} {page.url.lower()}" for token in ("just a moment", "one more step", "verify you are human", "cf-chl", "cloudflare"))
        if challenge and await page.locator("form").count() == 0:
            screenshot = diagnostics / "security_check.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            return "NEEDS_HUMAN", {"detection": detection.as_dict(), "result": {"status": "NEEDS_HUMAN", "current_url": page.url, "screenshot": str(screenshot), "details": {"reason": "Cloudflare security check blocked access before application form"}}, "questions": []}
        provider_name = detection.provider if detection.provider in PROVIDERS else provider_name
        provider = PROVIDERS.get(provider_name, PROVIDERS["generic"])()
        result = await provider.fill(page, job=job, resume_path=resume_path, answers=answers, submit=submit, output_dir=output_dir)
        status = result.status
        return result.status, {"detection": detection.as_dict(), "result": {"status": result.status, "current_url": result.current_url, "confirmation_text": result.confirmation_text, "details": result.details, "screenshot": str(result.screenshot) if result.screenshot else None}, "questions": [q.__dict__ for q in result.questions]}
    except Exception as exc:
        error_path = diagnostics / "browser_error.json"
        error_path.write_text(json.dumps({"error": str(exc), "url": getattr(handle.page, "url", "") if handle else ""}, indent=2), encoding="utf-8")
        return "NEEDS_HUMAN", {"detection": {"provider": provider_name}, "result": {"status": "NEEDS_HUMAN", "current_url": getattr(handle.page, "url", "") if handle else "", "details": {"reason": f"Browser execution error: {exc}", "error_artifact": str(error_path)}}, "questions": []}
    finally:
        if handle:
            if trace_started:
                try:
                    await handle.context.tracing.stop(path=str(diagnostics / "trace.zip"))
                except Exception:
                    pass
            # A headed review is an operator-facing session. Keep Chromium,
            # its context, and the page alive for NEEDS_INPUT/NEEDS_HUMAN and
            # the final pre-submit READY_TO_APPLY review.
            keep_session = headed and status in {"NEEDS_INPUT", "NEEDS_HUMAN", "READY_TO_APPLY"}
            await close_browser(handle, keep_session=keep_session)


def apply_job(*, job_id: str, submit: bool = False, headed: bool = True, force: bool = False, continue_run: bool = False, root: Path | None = None) -> ApplyResult:
    root = root or Path(__file__).resolve().parents[3]
    row = get_job(job_id)
    if not row:
        raise ValueError(f"Unknown job id: {job_id}")
    job = _job_dict(row)
    job["_root"] = root
    eligibility = classify_hard_eligibility(job, load_profile())
    update_job_eligibility(job_id, eligibility)
    if eligibility["status"] == "INELIGIBLE":
        reason = eligibility["reason"]
        transition_application(job_id, ApplicationState.INELIGIBLE_HARD_REQUIREMENT.value, event_type="hard_eligibility_failed", payload={"reason": reason, "hard_requirements": eligibility["hard_requirements"]}, force=force)
        update_application(job_id, last_step="hard_eligibility_gate", needs_human_reason=reason, notes=f"Skipped before resume/browser preparation: {reason}")
        return ApplyResult(job_id, ApplicationState.INELIGIBLE_HARD_REQUIREMENT.value, job.get("ats_provider") or "unknown", not submit, {"passed": False, "hard_eligibility": eligibility}, {}, {"reason": reason, "hard_eligibility": eligibility})
    output_dir = root / "output" / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = get_application(job_id)
    if existing and existing["state"] == ApplicationState.SUBMITTED.value and not force:
        raise ValueError("Application is already SUBMITTED; pass --force only for an explicit recovery path")
    # Build the tailored artifact on demand. Older versions required a manually
    # generated resume to exist before `apply`; batch mode must be self-contained.
    resume_candidates = [output_dir / name for name in ("resume_v4.pdf", "resume_v3.pdf", "resume.pdf")]
    if not any(path.exists() for path in resume_candidates):
        built = build_application(job=job, output_dir=output_dir)
        if not built.get("compiled"):
            raise RuntimeError(f"Could not compile tailored resume: {built.get('compile_log', '')[-1000:]}")
        pdf = Path(built["pdf"])
        validation = validate_pdf(pdf, required_terms=required_resume_terms(), forbidden_terms=forbidden_resume_terms())
        (output_dir / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
        if validation.get("errors"):
            raise RuntimeError(f"Tailored resume failed validation: {validation['errors']}")
        render_preview(pdf, output_dir / "resume_page1.png")
    final_pdf, manifest = promote_resume(output_dir=output_dir, job_id=job_id, company=job["company"], role=job["title"], source_url=job["url"])
    update_application(job_id, resume_final_path=str(final_pdf), resume_manifest_path=str(manifest))
    try:
        transition_application(job_id, ApplicationState.RESUME_READY.value, event_type="resume_promoted", payload={"path": str(final_pdf)}, force=force)
    except ValueError:
        if not continue_run:
            raise
    answers_path = PROFILE_DIR / "application_answers.yaml"
    unresolved: list[dict] = []
    if answers_path.exists():
        answers = AnswerVault.load(answers_path)
        private_dir = PROFILE_DIR / "private"
        reusable_private = PROFILE_DIR / "answers.private.yaml"
        if reusable_private.exists():
            answers = answers.overlay(reusable_private)
        private_job_answers = private_dir / f"{job_id}.yaml"
        answers = answers.overlay(private_job_answers)
    else:
        answers = None
        unresolved.append({"question": "Application answer vault", "category": "UNKNOWN", "required": True, "answer_source": "missing application_answers.yaml"})
    detection = detect_provider(job["url"])
    # Hydration may have observed the ATS only after following a redirect or
    # inspecting the page DOM. Reuse that trusted persisted signal on later
    # dry-runs and batch runs instead of downgrading the provider to unknown.
    if detection.provider == "unknown" and job.get("ats_provider") in PROVIDERS:
        detection = Detection(
            provider=job["ats_provider"], confidence=0.9,
            signals=("persisted during official-page hydration",),
            canonical_url=job.get("canonical_url") or job["url"],
            official_host_verified=bool(job.get("official_host_verified")),
        )
    duplicate = has_verified_submission(job_id)
    preflight = run_preflight(job=job, output_dir=output_dir, resume_path=final_pdf, manifest_path=manifest, detection=detection.as_dict(), unresolved=unresolved, duplicate=duplicate, submit=submit)
    _write_json(output_dir / "application_context.json", {"job_id": job_id, "company": job["company"], "role": job["title"], "url": job["url"], "provider": detection.as_dict()})
    record_application_artifact(job_id, "resume_final", str(final_pdf), sha256_file(final_pdf))
    update_application(job_id, ats_provider=detection.provider, canonical_application_url=detection.canonical_url, preflight_path=str(output_dir / "preflight.json"))
    if duplicate:
        update_application(job_id, last_step="duplicate_guard", needs_human_reason="A verified submission already exists for this company and role")
        transition_application(job_id, ApplicationState.NEEDS_HUMAN.value, event_type="duplicate_submission_blocked", payload={"duplicate": True}, force=force)
        return ApplyResult(job_id, ApplicationState.NEEDS_HUMAN.value, detection.provider, not submit, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, {"reason": "duplicate submission blocked"})
    if unresolved and not submit:
        update_application(job_id, answers_path=str(answers_path), last_step="dry_run_artifacts")
        transition_application(job_id, ApplicationState.READY_TO_APPLY.value, event_type="dry_run_ready", payload={"answer_vault": "not required for dry run"}, force=force)
        return ApplyResult(job_id, ApplicationState.READY_TO_APPLY.value, detection.provider, True, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, {"reason": "safe dry run; answer vault not required", "answer_vault": str(answers_path)})
    if unresolved:
        unresolved_path = write_unresolved(output_dir, unresolved, reason="APPLICATION_ANSWER_VAULT_MISSING")
        update_application(job_id, answers_path=str(answers_path), last_step="answer_vault", needs_human_reason="Create application_answers.yaml with verified reusable answers")
        transition_application(job_id, ApplicationState.NEEDS_INPUT.value, event_type="missing_answer_vault", payload={"path": str(answers_path)}, force=force)
        return ApplyResult(job_id, ApplicationState.NEEDS_INPUT.value, detection.provider, not submit, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest), "unresolved": str(unresolved_path)}, {"reason": "answer vault missing"})
    if not preflight["passed"] and submit:
        transition_application(job_id, ApplicationState.NEEDS_INPUT.value, event_type="preflight_failed", payload=preflight, force=force)
        return ApplyResult(job_id, ApplicationState.NEEDS_INPUT.value, detection.provider, False, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, {"reason": "preflight failed"})
    transition_application(job_id, ApplicationState.READY_TO_APPLY.value, event_type="preflight_passed", payload=preflight, force=force)
    # Headless dry-runs are artifact-only. A headed dry-run explicitly means
    # browser verification: fill and inspect, but never submit.
    if not submit and not headed:
        return ApplyResult(job_id, ApplicationState.READY_TO_APPLY.value, detection.provider, True, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, {"reason": "safe dry run; browser not opened"})
    transition_application(job_id, ApplicationState.APPLYING.value, event_type="browser_started", force=force)
    try:
        if detection.provider in {"greenhouse", "lever", "ashby", "workday"}:
            status, browser_details = _run_extension(job=job, output_dir=output_dir, resume_path=final_pdf, answers=answers, submit=submit)
        else:
            status, browser_details = asyncio.run(_run_browser(job=job, output_dir=output_dir, provider_name=detection.provider, resume_path=final_pdf, answers=answers, submit=submit, headed=headed))
    except BrowserUnavailable as exc:
        update_application(job_id, last_error=str(exc), needs_human_reason="Install Playwright and Chromium")
        transition_application(job_id, ApplicationState.NEEDS_HUMAN.value, event_type="browser_unavailable", payload={"error": str(exc)}, force=force)
        return ApplyResult(job_id, ApplicationState.NEEDS_HUMAN.value, detection.provider, False, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, {"error": str(exc)})
    _write_json(output_dir / "browser_result.json", browser_details)
    questions = browser_details.get("questions", [])
    for question in questions:
        record_application_question(job_id, question)
    if status == "NEEDS_INPUT":
        unresolved_path = write_unresolved(output_dir, questions)
        update_application(job_id, last_step="form_fill", needs_human_reason="Required application fields need verified answers")
        transition_application(job_id, ApplicationState.NEEDS_INPUT.value, event_type="required_questions", payload={"count": len(questions)}, force=force)
        provider = browser_details.get("detection", {}).get("provider", detection.provider)
        return ApplyResult(job_id, ApplicationState.NEEDS_INPUT.value, provider, not submit, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest), "unresolved": str(unresolved_path)}, browser_details)
    if status == "NEEDS_HUMAN":
        reason = browser_details.get("result", {}).get("details", {}).get("reason", "human review required")
        update_application(job_id, last_step="browser_review", needs_human_reason=reason, confirmation_screenshot_path=browser_details.get("result", {}).get("screenshot"))
        transition_application(job_id, ApplicationState.NEEDS_HUMAN.value, event_type="human_review_required", payload={"reason": reason}, force=force)
        provider = browser_details.get("detection", {}).get("provider", detection.provider)
        return ApplyResult(job_id, ApplicationState.NEEDS_HUMAN.value, provider, not submit, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, browser_details)
    update_application(job_id, last_url=browser_details.get("result", {}).get("current_url"), last_step="form_filled")
    if status == "SUBMITTED":
        result_detail = browser_details.get("result", {})
        receipt = write_receipt(output_dir, {"job_id": job_id, "company": job["company"], "role": job["title"], "ats": browser_details.get("detection", {}).get("provider", detection.provider), "application_url": result_detail.get("current_url", job["url"]), "resume_path": str(final_pdf), "resume_sha256": sha256_file(final_pdf), "answers_path": str(answers.path), "confirmation_text": result_detail.get("confirmation_text", ""), "confirmation_screenshot": result_detail.get("screenshot"), "submission_method": "JOB_BEAST_COMPANION", "status": "SUBMITTED"})
        update_application(job_id, submission_receipt_path=str(receipt), submitted_at=datetime.now(timezone.utc).isoformat(), submission_method="JOB_BEAST_COMPANION")
        transition_application(job_id, ApplicationState.SUBMITTED.value, event_type="submission_verified", payload={"receipt": str(receipt)}, force=force)
        return ApplyResult(job_id, ApplicationState.SUBMITTED.value, detection.provider, False, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest), "receipt": str(receipt)}, browser_details)
    if status == "READY_TO_APPLY" and not submit:
        screenshot = browser_details.get("result", {}).get("screenshot")
        update_application(
            job_id,
            last_url=browser_details.get("result", {}).get("current_url"),
            last_step="browser_dry_run_verified",
            confirmation_screenshot_path=screenshot,
        )
        transition_application(job_id, ApplicationState.READY_TO_APPLY.value, event_type="browser_dry_run_verified", payload={"provider": browser_details.get("detection", {}).get("provider")}, force=force)
        provider = browser_details.get("detection", {}).get("provider", detection.provider)
        return ApplyResult(job_id, ApplicationState.READY_TO_APPLY.value, provider, True, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest), "screenshot": screenshot}, browser_details)
    return ApplyResult(job_id, ApplicationState.NEEDS_HUMAN.value, detection.provider, False, preflight, {"resume_final": str(final_pdf), "manifest": str(manifest)}, {"reason": "filled without submit; explicit submit verification still required", **browser_details})


def run_apply(**kwargs) -> dict:
    return apply_job(**kwargs).as_dict()
