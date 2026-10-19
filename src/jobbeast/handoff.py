"""Claude handoff layer.

Prepares a self-contained application packet for an external AI browser
operator (Claude) to execute, instead of this repo's own Playwright/Chrome-
extension executor. Job Beast's job ends at READY_FOR_CLAUDE; the live
application website is Claude's job; import_result() is how Job Beast
records what happened.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .apply.answers import AnswerVault
from .apply.artifacts import promote_resume, sha256_file
from .apply.questions import classify_question
from .apply.receipts import write_receipt
from .batch import hydrate_job
from .eligibility import classify_hard_eligibility
from .profile import PROFILE_DIR, forbidden_resume_terms, load_profile, required_resume_terms
from .radar import OFF_TARGET, effective_freshness, rerank_queue
from .resume_builder import build_application
from .scorer import score_job
from .store import (
    ensure_application, get_application, get_job, has_verified_submission, is_aggregator_url,
    park_queue, queue_row, record_application_artifact, transition_application, update_application,
    update_job_eligibility, update_queue_eligibility,
)
from .tailor import choose_projects
from .validation import render_preview, validate_pdf

ROOT = Path(__file__).resolve().parents[2]
ANSWER_VAULT_PATH = PROFILE_DIR / "application_answers.yaml"
PRIVATE_ANSWERS_PATH = PROFILE_DIR / "answers.private.yaml"

def _answer_vault_path() -> Path:
    """The candidate's answer vault, or the profile's example template if none exists yet.

    The template answers everything sensitive with REQUIRES_USER_INPUT /
    DECLINE_TO_ANSWER, so falling back to it can only make the operator stop
    and ask -- never guess.
    """
    if ANSWER_VAULT_PATH.exists():
        return ANSWER_VAULT_PATH
    template = ANSWER_VAULT_PATH.with_name("application_answers.example.yaml")
    return template if template.exists() else ANSWER_VAULT_PATH


# Demographic/EEO data never leaves the local vault -- it is filtered out
# before any packet-building code runs, not after.
EEO_PREFIXES = ("demographics.", "voluntary_eeo.")

# Known legal/eligibility-sensitive vault keys and the JD text signal(s) that
# justify including them in a given job's packet. A verified legal answer
# that isn't signaled by the JD is omitted (not marked unknown) -- Claude's
# instructions still cover the case where the live page asks anyway.
LEGAL_KEYS: dict[str, set[str]] = {
    "work_preferences.work_authorization": {"work_authorization"},
    "work_preferences.sponsorship_now": {"sponsorship"},
    "work_preferences.sponsorship_future": {"sponsorship"},
    "work_preferences.sponsorship_required": {"sponsorship"},
    "legal.immigration_status": {"immigration", "export_control", "clearance"},
    "legal.work_authorization_answer_requires_employer_review": {"work_authorization"},
}
LEGAL_TEXT_SIGNALS: dict[str, tuple[str, ...]] = {
    "work_authorization": ("authorized to work", "work authorization", "legally eligible to work", "eligible to work"),
    "sponsorship": ("sponsorship", "visa sponsorship", "sponsor "),
    "immigration": ("immigration status", "citizenship", "permanent resident", "green card"),
    "export_control": ("export control", "itar", "ear eligib", "u.s. person"),
    "clearance": ("security clearance", "clearance eligib"),
}
STOP_FOR_USER_CATEGORIES = {"WORK_AUTHORIZATION", "SPONSORSHIP"}
# Matches hydration.py's own HYDRATED threshold -- the bar for "enough JD text
# exists to tailor truthfully", independent of whether the latest live fetch
# itself cleared that bar.
JD_SUFFICIENT_CHARS = 300


class HandoffError(RuntimeError):
    """Raised when a job cannot be prepared for Claude handoff.

    Carries a machine-readable `reason` so handoff_batch() can record a
    per-job failure and keep going rather than raising past the caller.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _job_dict(row) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys()}
    try:
        result["location"] = json.loads(result.get("locations_json") or "[]")[0]
    except (IndexError, TypeError, ValueError):
        result["location"] = ""
    return result


def _is_eeo_key(key: str) -> bool:
    # Also the demographics *policy* flag: no EEO-related key is packet data.
    return any(key.startswith(prefix) for prefix in EEO_PREFIXES) or "demographic" in key or "eeo" in key.split(".")[-1]


def _legal_categories_signaled(description: str) -> set[str]:
    low = (description or "").lower()
    return {category for category, needles in LEGAL_TEXT_SIGNALS.items() if any(needle in low for needle in needles)}


def _candidate_profile() -> dict[str, Any]:
    profile = load_profile()
    return {
        "name": profile.get("name"),
        "headline": profile.get("headline"),
        "education": profile.get("education", {}),
        "links": profile.get("links", {}),
        "contact": profile.get("contact", {}),
    }


def _split_answers(vault: AnswerVault, *, description: str) -> dict[str, Any]:
    """Split the flattened answer vault into packet-safe buckets.

    EEO/demographic keys are dropped before any bucket is built -- they are
    never written to candidate_profile, verified_application_answers,
    legal_sensitive_answers, or unknown_answers, and are never used for
    eligibility/ranking/resume content (those modules never read the vault).
    """
    flattened = {key: value for key, value in vault.flattened().items() if not _is_eeo_key(key)}
    verified: dict[str, Any] = {}
    legal: dict[str, Any] = {}
    unknown: list[dict[str, Any]] = []
    for key, value in sorted(flattened.items()):
        legal_categories = LEGAL_KEYS.get(key, set())
        is_legal = bool(legal_categories)
        answer = vault.known(key)
        if answer is None:
            label = key.replace(".", " ").replace("_", " ")
            question = classify_question(label)
            recommended = "STOP_FOR_USER" if (is_legal or question.category in STOP_FOR_USER_CATEGORIES) else "DRAFT_FROM_EVIDENCE"
            unknown.append({
                "question": key,
                "category": "LEGAL_SENSITIVE" if is_legal else question.category,
                "reason_unresolved": "not set in answer vault",
                "sensitive": is_legal,
                "required": is_legal,
                "recommended_action": recommended,
            })
            continue
        if is_legal:
            # User-confirmed legal answers are always available to the operator:
            # application forms ask them whether or not the JD mentions them.
            # Reuse is restricted to questions with the same material meaning
            # (see the operator instructions); anything else is NEEDS_INPUT.
            legal[key] = answer.value
        else:
            verified[key] = answer.value
    return {"verified_application_answers": verified, "legal_sensitive_answers": legal, "unknown_answers": unknown}


def _source_hash(*, jd_hash: str, answers: dict[str, Any], project_names: list[str], submission_authorized: bool = False) -> str:
    payload = {
        "submission_authorized": bool(submission_authorized),
        "jd_hash": jd_hash or "",
        "verified_answers": answers.get("verified_application_answers", {}),
        "legal_answers": answers.get("legal_sensitive_answers", {}),
        "unknown_questions": sorted(item["question"] for item in answers.get("unknown_answers", [])),
        "projects": sorted(project_names),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_context_md(path: Path, *, packet: dict[str, Any]) -> str:
    job = packet["job"]
    verified = packet["verified_application_answers"]
    legal = packet["legal_sensitive_answers"]
    unknown = packet["unknown_answers"]
    lines = [
        f"# {job['company']} -- {job['title']}",
        "",
        "## Application",
        f"- Official URL: {packet['canonical_application_url']}",
        f"- Original source URL: {packet['original_source_url']}",
        f"- Job ID: {packet['job_id']}",
        f"- ATS provider: {packet['provider']}" + (f" (job id: {packet['provider_job_id']})" if packet.get("provider_job_id") else ""),
        f"- Fit score: {packet['fit_score']} / 100 -- freshness: {packet['freshness']['bucket']}",
    ]
    lines.append(f"- JD source: {packet.get('jd_hydration_state', 'OFFICIAL_PAGE')} -- application route: {packet.get('application_route', 'EXTERNAL')}")
    if not packet.get("jd_source_verified", True):
        lines.append("- NOTE: the JD text used to tailor this resume did not come from a confirmed read of the employer's live page. Verify the live posting's actual requirements before relying on anything not stated here.")
    lines += [
        "",
        "## Approved resume",
        f"- {packet['absolute_resume_path']}",
        f"- sha256: {packet['resume_sha256']}",
        "",
        "## Verified contact information",
        f"- Name: {packet['candidate_profile'].get('name')}",
        f"- Email: {packet['candidate_profile'].get('contact', {}).get('email')}",
        f"- Phone: {packet['candidate_profile'].get('contact', {}).get('phone')}",
        "",
        "## Verified standard application answers",
    ]
    lines += [f"- {key}: {value}" for key, value in sorted(verified.items())] or ["(none)"]
    lines += ["", "## User-confirmed legal/eligibility answers (reuse ONLY for a question with the same material meaning)"]
    lines += [f"- {key}: {value}" for key, value in sorted(legal.items())] or ["(none provided -- NEEDS_INPUT if the live page asks)"]
    lines += ["", "## Questions that require user review"]
    lines += [f"- [{item['recommended_action']}] {item['question']} ({item['category']})" for item in unknown] or ["(none)"]
    lines += [
        "",
        "## Submission rules",
        f"- submission_authorized: {str(bool(packet.get('submission_authorized'))).lower()}"
        + (" -- submit without asking again once every pre-submit check passes." if packet.get("submission_authorized") else " -- prepare and verify everything, but do NOT click Submit."),
        "- Never invent citizenship, immigration status, work authorization, sponsorship, export-control eligibility, security-clearance answers, or legal attestations.",
        "- CAPTCHA: complete everything else, then report READY_FOR_HUMAN_CAPTCHA. MFA: report READY_FOR_HUMAN_MFA. Never solve or bypass either.",
        "- Fill output/<job_id>/claude_result.json with the outcome when done.",
    ]
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_prompt_md(path: Path, *, packet: dict[str, Any], packet_path: Path, context_path: Path, resume_path: Path, result_path: Path) -> None:
    authorized = bool(packet.get("submission_authorized"))
    route = packet.get("application_route") or "EXTERNAL"
    route_note = ""
    if route != "EXTERNAL":
        route_note = (
            "    This job was found on LinkedIn without an employer apply link. Open the LinkedIn posting,\n"
            "    then follow its employer \"Apply\" button to the employer's own application page. If the\n"
            "    only route is LinkedIn Easy Apply (requires the user's LinkedIn session), report NEEDS_INPUT\n"
            "    with failure_class LINKEDIN_EASY_APPLY_REQUIRES_USER. Never log in for the user.\n"
        )
    jd_note = (
        "    Our JD text did not come from a confirmed read of the employer's live page: cross-check the\n"
        "    live requirements (degree level, graduation window, location, authorization) before filling.\n"
        if not packet.get("jd_source_verified", True) else ""
    )
    submit_rule = (
        "10. submission_authorized is TRUE: when every check in step 9 passes, click Submit without asking\n"
        "    for another confirmation.\n"
        if authorized else
        "10. submission_authorized is FALSE: stop after step 9. Do not click Submit. Report status\n"
        "    READY_TO_SUBMIT with the verified final state.\n"
    )
    text = f"""# Claude browser operator instructions

1. Read {context_path} for the full job context ({packet_path} is the machine-readable packet).
2. Open the application URL listed there.
{route_note}{jd_note}3. Live posting recheck: confirm the employer and role match the packet. If the live posting states a
   hard requirement the candidate does not meet (graduate-only enrollment, graduation window,
   citizenship/clearance), stop: do not upload or fill anything. Report INELIGIBLE_HARD_REQUIREMENT
   with the requirement code in failure_reason and the quoted live text in live_posting_evidence.
   If the posting is gone, report CLOSED.
4. Use only information supplied in the context/packet. Do not invent data.
5. Upload exactly this resume: {resume_path} (sha256 {packet.get('resume_sha256')}). The upload counts
   only when the employer's UI visibly shows the attached file (file name / attached state) --
   hidden DOM text alone is not proof. If the widget will not take the file, report FAILED with
   failure_class FILE_UPLOAD.
6. Fill every field with a verified answer; use semantic equivalence only for ordinary wording
   variations. For custom dropdowns, select and then read the displayed value back.
7. Legal/eligibility questions (citizenship, immigration, work authorization, sponsorship, export
   control, clearance, attestations): reuse a user-confirmed answer ONLY when the question has the
   same material meaning. If the wording materially changes, report NEEDS_INPUT. Never infer.
   Voluntary EEO self-identification questions: answer from the user-confirmed values in
   the profile's answers.private.yaml (voluntary_eeo). Never copy those values into the result,
   notes, logs, or any other file.
8. Employer-specific written questions: draft from the packet's verified evidence when the
   recommended_action is DRAFT_FROM_EVIDENCE; otherwise report NEEDS_INPUT.
9. Final review before submit, all must hold: correct employer, correct role, eligibility recheck
   passed, the approved resume visibly attached, every required field complete and read back,
   zero validation errors, no unresolved legal/sensitive question, no CAPTCHA, no MFA.
{submit_rule}11. CAPTCHA: never solve, bypass, or automate it. Complete everything else first, then report
    READY_FOR_HUMAN_CAPTCHA and move on to the next job. MFA: same, report READY_FOR_HUMAN_MFA.
    After the user completes it, re-verify step 9 and (if authorized) submit immediately.
12. After clicking Submit, a click alone is not success: require the employer's confirmation page or
    message. Only then report SUBMITTED with confirmation_detected=true and the confirmation text.
    If the submit appears to have gone through but no confirmation can be verified, report
    SUBMITTED_UNVERIFIED (submitted=true, confirmation_detected=false) and never retry it.
    If the submit clearly failed, report FAILED.
13. Any failure (layout change, navigation, unsupported control, upload, timeout, broken posting,
    unknown question): record the exact failure_class and failure_reason and move on. Do not debug.
14. Write the outcome to {result_path} using the schema already present in that file.
"""
    path.write_text(text, encoding="utf-8")


def _write_result_template(path: Path, *, job_id: str, packet_version: int, resume_sha256: str) -> None:
    template = {
        "job_id": job_id,
        "packet_version": packet_version,
        "resume_sha256": resume_sha256,
        "status": None,
        "final_url": None,
        "submitted": None,
        "confirmation_detected": None,
        "confirmation_text": None,
        "submitted_at": None,
        "unresolved_questions": [],
        "failure_class": None,
        "failure_reason": None,
        "live_posting_evidence": None,
        "evidence_paths": [],
    }
    path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_operator_instruction(path: Path, *, job_id: str, company: str, title: str, official_application_url: str, context_path: Path, packet_path: Path, resume_path: Path, result_path: Path) -> None:
    text = "\n".join([
        f"JOB BEAST -> CLAUDE HANDOFF: {company} -- {title}",
        f"job_id: {job_id}",
        f"official_application_url: {official_application_url}",
        f"context: {context_path}",
        f"packet: {packet_path}",
        f"resume: {resume_path}",
        f"result (Claude fills this in when done): {result_path}",
        "",
        "Start a Claude browser-operating session and give it the context + prompt file paths above.",
    ]) + "\n"
    path.write_text(text, encoding="utf-8")


# Words that only appear on a resume if private/EEO/immigration data leaked in.
_PRIVATE_LEAK = re.compile(r"\b(?:gender|pronouns?|ethnicity|hispanic|latino|veteran|disabilit(?:y|ies)|f-1|visa|sponsorship|citizenship|immigration)\b", re.I)
MIN_RESUME_SKILLS = 6


def resume_quality_errors(text: str, *, coverage: dict[str, Any], company: str, title: str) -> list[str]:
    """Checks beyond page/text/overlap validation: identity, duplication, leakage, tailoring."""
    errors: list[str] = []
    profile = load_profile()
    contact = profile.get("contact", {})
    flat = " ".join(text.split())
    if profile.get("name") and profile["name"] not in flat:
        errors.append("candidate name missing")
    if contact.get("email") and contact["email"].lower() not in flat.lower():
        errors.append("candidate email missing")
    phone_digits = re.sub(r"\D", "", str(contact.get("phone") or ""))
    if phone_digits and phone_digits not in re.sub(r"\D", "", text):
        errors.append("candidate phone missing")
    bullets = [" ".join(line.strip().lstrip("•·-–").split()).lower() for line in text.splitlines() if line.strip().startswith(("•", "·", "–"))]
    duplicates = sorted({b for b in bullets if b and bullets.count(b) > 1})
    if duplicates:
        errors.append(f"duplicate bullets: {duplicates[:3]}")
    leaked = sorted({m.group(0).lower() for m in _PRIVATE_LEAK.finditer(text)})
    if leaked:
        errors.append(f"private/EEO/immigration terms on resume: {leaked}")
    skills = coverage.get("included_skills") or {}
    groups = skills.values() if isinstance(skills, dict) else [skills]
    # Each category is rendered as a comma-joined string ("Python, Java, SQL").
    skill_count = sum(len([x for x in (g.split(",") if isinstance(g, str) else g) if str(x).strip()]) for g in groups)
    if skill_count < MIN_RESUME_SKILLS:
        errors.append(f"Technical Skills collapsed to {skill_count} item(s); need >= {MIN_RESUME_SKILLS}")
    job = coverage.get("job") or {}
    if job.get("company") != company or job.get("title") != title:
        errors.append(f"resume tailored for {job.get('company')!r}/{job.get('title')!r}, not {company!r}/{title!r}")
    return errors


def jd_hydration_state(row) -> str:
    """Where the JD text used for tailoring came from.

    OFFICIAL_PAGE: fetched from the employer/ATS page. TRUSTED_SOURCE_DESCRIPTION:
    a structured source's own description (e.g. LinkedIn listing text).
    JS_RENDERED_LIVE_VERIFICATION_REQUIRED: the employer page did not render to
    static HTTP; enough trusted text exists, but Claude must verify live.
    """
    if row["hydration_status"] != "HYDRATED":
        return "JS_RENDERED_LIVE_VERIFICATION_REQUIRED"
    from_listing = "apify linkedin listing" in (row["ats_provider_evidence"] or "") or is_aggregator_url(row["official_url"] or row["url"])
    return "TRUSTED_SOURCE_DESCRIPTION" if from_listing else "OFFICIAL_PAGE"


def prepare_handoff(job_id: str, *, force: bool = False, root: Path | None = None, submission_authorized: bool = False) -> dict[str, Any]:
    root = root or ROOT
    row = get_job(job_id)
    if not row:
        raise HandoffError("UNKNOWN_JOB", job_id)
    ensure_application(job_id)

    if not row["active"]:
        raise HandoffError("CLOSED", f"{row['company']} / {row['title']} listing is no longer active")

    if has_verified_submission(job_id):
        raise HandoffError("ALREADY_SUBMITTED", f"{row['company']} / {row['title']} already has a verified submission")

    queued = queue_row(job_id)
    if queued and queued["status"] == "SKIPPED" and queued["last_error"] == OFF_TARGET:
        raise HandoffError("SKIPPED_OFF_TARGET", f"{row['company']} / {row['title']} is not a technical target role")

    # Only re-fetch if we don't already have enough trusted JD content -- a
    # stale `hydration_status` from a prior incomplete fetch isn't itself a
    # reason to re-hit the network when the stored description (which never
    # shrinks, see update_job_hydration) already clears the bar.
    if len(row["description"] or "") < JD_SUFFICIENT_CHARS:
        try:
            hydrate_job(job_id, force=force)
        except Exception as exc:  # noqa: BLE001 - classified below, never fatal to the batch
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                raise HandoffError("URL_INVALID", f"official posting returned HTTP {status_code}") from exc
            # Anything else (timeouts, bot walls, JS-only pages) falls through to
            # the content-length check: too little text is NEEDS_HYDRATION.
        row = get_job(job_id)

    job = _job_dict(row)
    profile = load_profile()
    # Always recheck against the current hydrated JD, even if the radar
    # already classified this job earlier -- a stale queue row must never
    # bypass this gate. INELIGIBLE/NEEDS_CONFIRMATION both block handoff.
    eligibility = classify_hard_eligibility(job, profile)
    if eligibility["status"] != "ELIGIBLE":
        if eligibility["status"] != row["eligibility_status"]:
            # The recheck disagrees with the stored verdict (e.g. an
            # eligibility.py fix now catches something the radar missed) --
            # correct it so this job stops being resurfaced as ELIGIBLE.
            update_job_eligibility(job_id, eligibility)
            update_queue_eligibility(job_id, eligibility["status"])
        if eligibility["status"] == "INELIGIBLE":
            # Demote a stale READY_FOR_CLAUDE (or earlier) application so an
            # already-written packet can't be picked up and operated.
            transition_application(job_id, "INELIGIBLE_HARD_REQUIREMENT", event_type="eligibility_recheck", payload=eligibility)
        raise HandoffError(eligibility["status"], eligibility["reason"])

    canonical_url = row["canonical_url"] or row["official_url"] or row["url"]
    if not canonical_url:
        # A genuine URL problem -- no page to send Claude to at all. Distinct
        # from "the page needs JS to render" (NEEDS_HYDRATION, below): this is
        # reserved for an actual missing/broken reference, not a thin scrape.
        raise HandoffError("URL_INVALID", "no canonical or official application URL available")

    # `hydration_status` reflects only the MOST RECENT fetch attempt; the
    # stored description can still hold enough trusted JD content from an
    # earlier fetch or the source listing's own metadata (update_job_hydration
    # never shrinks it). So gate on actual content length, not that flag --
    # a page that merely needs JS to render its full text should not be
    # treated the same as a broken/wrong URL.
    description_len = len(row["description"] or "")
    hydration_state = jd_hydration_state(row)
    jd_source_verified = hydration_state == "OFFICIAL_PAGE"
    if description_len < JD_SUFFICIENT_CHARS:
        raise HandoffError("NEEDS_HYDRATION", f"only {description_len} chars of JD content available (need >={JD_SUFFICIENT_CHARS}); the live posting may require JS rendering this pass's static HTTP hydration can't follow")

    output_dir = root / "output" / job_id
    projects = choose_projects(job)
    vault = AnswerVault.load(_answer_vault_path()).overlay(PRIVATE_ANSWERS_PATH)
    answers = _split_answers(vault, description=job.get("description", ""))
    project_names = [p.get("name", "") for p in projects]
    # Hash only the INPUTS (JD, answers, chosen projects) -- never the compiled
    # PDF's own bytes, which embed a build timestamp and would never compare
    # equal across two runs even when nothing meaningful changed.
    new_hash = _source_hash(jd_hash=row["jd_hash"] or "", answers=answers, project_names=project_names, submission_authorized=submission_authorized)

    app = get_application(job_id)
    current_version = int(app["handoff_version"] or 0) if app else 0
    packet_path = output_dir / "application_packet.json"
    context_path = output_dir / "application_context.md"
    prompt_path = output_dir / "claude_browser_prompt.md"
    result_path = output_dir / "claude_result.json"
    resume_path = output_dir / "resume_final.pdf"
    operator_path = output_dir / "operator_instruction.md"
    unchanged = (
        not force
        and app is not None
        and app["handoff_source_hash"] == new_hash
        and packet_path.exists() and context_path.exists() and prompt_path.exists()
        and result_path.exists() and resume_path.exists() and operator_path.exists()
    )
    if unchanged:
        existing_packet = json.loads(packet_path.read_text(encoding="utf-8"))
        return {
            "status": "UNCHANGED", "job_id": job_id, "company": row["company"], "title": row["title"],
            "fit_score": existing_packet.get("fit_score"), "freshness": existing_packet.get("freshness"),
            "packet_version": current_version, "resume_sha256": existing_packet.get("resume_sha256"),
            "packet_path": str(packet_path), "context_path": str(context_path),
            "prompt_path": str(prompt_path), "result_template_path": str(result_path),
            "resume_path": str(resume_path), "official_application_url": canonical_url,
            "operator_instruction_path": str(operator_path),
        }

    report = score_job(job)
    built = build_application(job=job, output_dir=output_dir)
    if not built.get("compiled"):
        raise HandoffError("FAILED_PREP", "resume build failed: " + built.get("compile_log", "")[-1000:])

    pdf_path = Path(built["pdf"])
    validation = validate_pdf(pdf_path, required_terms=required_resume_terms(), forbidden_terms=forbidden_resume_terms())
    coverage = json.loads(Path(built["report"]).read_text(encoding="utf-8")) if Path(built["report"]).exists() else {}
    validation["quality_errors"] = resume_quality_errors(validation.get("text", ""), coverage=coverage, company=row["company"], title=row["title"])
    (output_dir / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    if validation.get("errors") or validation["quality_errors"]:
        raise HandoffError("FAILED_PREP", "resume validation failed: " + "; ".join(validation.get("errors", []) + validation["quality_errors"]))
    render_preview(pdf_path, output_dir / "resume_page1.png")

    final_resume, manifest = promote_resume(output_dir=output_dir, job_id=job_id, company=row["company"], role=row["title"], source_url=canonical_url)
    resume_sha256 = sha256_file(final_resume)

    packet_version = current_version + 1
    generated_at = datetime.now(timezone.utc).isoformat()
    packet = {
        "job_id": job_id,
        "company": row["company"],
        "role": row["title"],
        "original_source_url": row["url"],
        "canonical_application_url": canonical_url,
        "provider": row["ats_provider"] or "unknown",
        "provider_job_id": row["provider_job_id"],
        "fit_score": report["score"],
        "freshness": effective_freshness(row),
        "eligibility_result": eligibility,
        "jd_source_verified": jd_source_verified,
        "jd_hydration_state": hydration_state,
        "application_route": json.loads(row["metadata_json"] or "{}").get("application_route") or ("LINKEDIN_PAGE_ONLY" if is_aggregator_url(canonical_url) else "EXTERNAL"),
        "source_url": json.loads(row["metadata_json"] or "{}").get("source_url") or row["url"],
        "resume_path": f"output/{job_id}/resume_final.pdf",
        "absolute_resume_path": str(final_resume),
        "resume_sha256": resume_sha256,
        "candidate_profile": _candidate_profile(),
        "verified_application_answers": answers["verified_application_answers"],
        "legal_sensitive_answers": answers["legal_sensitive_answers"],
        "unknown_answers": answers["unknown_answers"],
        "submission_authorized": bool(submission_authorized),
        "packet_version": packet_version,
        "context_sha256": None,
        "generated_at": generated_at,
        "job": {"company": row["company"], "title": row["title"], "description_excerpt": (job.get("description") or "")[:1000]},
    }
    packet_hash = _write_json(packet_path, packet)
    context_hash = _write_context_md(context_path, packet=packet)
    packet["context_sha256"] = context_hash
    packet_hash = _write_json(packet_path, packet)
    _write_prompt_md(prompt_path, packet=packet, packet_path=packet_path, context_path=context_path, resume_path=final_resume, result_path=result_path)
    _write_result_template(result_path, job_id=job_id, packet_version=packet_version, resume_sha256=resume_sha256)
    _write_operator_instruction(
        operator_path, job_id=job_id, company=row["company"], title=row["title"], official_application_url=canonical_url,
        context_path=context_path, packet_path=packet_path, resume_path=final_resume, result_path=result_path,
    )

    update_application(
        job_id,
        resume_final_path=str(final_resume), resume_manifest_path=str(manifest),
        handoff_version=packet_version, handoff_created_at=generated_at, handoff_source_hash=new_hash,
    )
    record_application_artifact(job_id, "handoff_packet", str(packet_path), sha256=packet_hash, metadata={"packet_version": packet_version})
    # Walk the ordinary pipeline states so READY_FOR_CLAUDE is reached through a
    # valid edge; a previously-advanced application remains authoritative, so
    # skip any step already behind the current state.
    for step in ("RESUME_READY", "READY_TO_APPLY"):
        try:
            transition_application(job_id, step, event_type="handoff_prepared")
        except ValueError:
            pass
    transition_application(job_id, "READY_FOR_CLAUDE", event_type="handoff_prepared", payload={"packet_version": packet_version, "resume_sha256": resume_sha256})

    return {
        "status": "READY_FOR_CLAUDE", "job_id": job_id, "company": row["company"], "title": row["title"],
        "fit_score": report["score"], "freshness": packet["freshness"], "packet_version": packet_version,
        "resume_sha256": resume_sha256, "packet_path": str(packet_path), "context_path": str(context_path),
        "prompt_path": str(prompt_path), "result_template_path": str(result_path),
        "resume_path": str(final_resume), "official_application_url": canonical_url,
        "operator_instruction_path": str(operator_path),
    }


# States a job sits in while actively or already handed off to Claude, or
# otherwise not a sensible target for automatic next-job selection. Shared by
# handoff_batch() and the CLI's `next`/`operate-next` so repeated calls
# advance through the queue instead of re-selecting the same job forever.
NEXT_EXCLUDED_STATES = {"SUBMITTED", "SUBMITTED_UNVERIFIED", "CLAUDE_SUBMITTED", "CLAUDE_RUNNING", "READY_FOR_CLAUDE", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_HUMAN_MFA", "INELIGIBLE_HARD_REQUIREMENT", "CLOSED"}


def select_queued_jobs(limit: int, *, include_needs_input: bool = False, scan_multiplier: int = 6):
    """Return up to `limit` queued rows not already handled, preserving queue order.

    Deliberately does NOT pre-filter cross-job duplicates (has_verified_submission) --
    prepare_handoff() is the sole authority on that check, so a duplicate still gets
    attempted and lands in handoff_batch()'s SKIPPED_DUPLICATE bucket instead of
    silently vanishing before it's ever counted.
    """
    from .store import queued_jobs

    excluded = set(NEXT_EXCLUDED_STATES)
    if not include_needs_input:
        excluded.add("CLAUDE_NEEDS_INPUT")
    selected = []
    for row in queued_jobs(limit=limit * scan_multiplier):
        if len(selected) >= limit:
            break
        job_id = row["job_id"]
        app = get_application(job_id)
        state = app["state"] if app and app["state"] else "DISCOVERED"
        if state in excluded:
            continue
        selected.append(row)
    return selected


# HandoffError.reason -> the batch-accounting status bucket it belongs to.
# Anything not listed here is a genuine technical failure ("FAILED").
_STATUS_FOR_REASON = {
    "INELIGIBLE": "INELIGIBLE",
    "NEEDS_CONFIRMATION": "NEEDS_CONFIRMATION",
    "ALREADY_SUBMITTED": "SKIPPED_DUPLICATE",
    "CLOSED": "CLOSED",
    "NEEDS_HYDRATION": "NEEDS_HYDRATION",
    "URL_INVALID": "URL_INVALID",
    "FAILED_PREP": "FAILED_PREP",
}
_BATCH_BUCKET_FOR_STATUS = {
    "READY_FOR_CLAUDE": "ready_for_claude", "UNCHANGED": "ready_for_claude",
    "FAILED": "failed", "NEEDS_HYDRATION": "failed", "URL_INVALID": "failed", "FAILED_PREP": "failed",
    "INELIGIBLE": "ineligible", "NEEDS_CONFIRMATION": "needs_confirmation",
    "SKIPPED_DUPLICATE": "skipped_duplicate", "CLOSED": "closed",
}
# Queue status a job is parked in after a handoff attempt, so it is never
# silently re-selected by every later batch.
_QUEUE_STATUS_FOR_STATUS = {
    "READY_FOR_CLAUDE": "READY_FOR_CLAUDE", "UNCHANGED": "READY_FOR_CLAUDE",
    "INELIGIBLE": "INELIGIBLE", "NEEDS_CONFIRMATION": "NEEDS_CONFIRMATION",
    "SKIPPED_DUPLICATE": "SKIPPED", "CLOSED": "CLOSED",
    "NEEDS_HYDRATION": "NEEDS_HYDRATION", "URL_INVALID": "URL_INVALID", "FAILED_PREP": "FAILED_PREP", "FAILED": "FAILED",
}


def handoff_job_ids(job_ids: list[str], *, root: Path | None = None, submission_authorized: bool = False,
                    rows: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prepare the given jobs independently. A failure preparing one job never
    stops the rest. Every attempted job lands in exactly one accounting bucket
    and is parked in a matching queue status: attempted always equals
    ready_for_claude + failed + ineligible + needs_confirmation +
    skipped_duplicate + closed -- no job may disappear from the count."""
    root = root or ROOT
    attempted: list[dict[str, Any]] = []
    for job_id in job_ids:
        row = (rows or {}).get(job_id) or get_job(job_id)
        company, title = (row["company"], row["title"]) if row else ("?", "?")
        try:
            result = prepare_handoff(job_id, root=root, submission_authorized=submission_authorized)
            item = {"job_id": job_id, "company": company, "title": title, **result}
        except HandoffError as exc:
            status = _STATUS_FOR_REASON.get(exc.reason, "FAILED")
            item = {"job_id": job_id, "company": company, "title": title, "status": status, "reason": exc.reason, "detail": exc.detail}
        except Exception as exc:  # noqa: BLE001 - one bad job must never stop the batch
            item = {"job_id": job_id, "company": company, "title": title, "status": "FAILED", "reason": "UNEXPECTED_ERROR", "detail": str(exc)}
        try:
            from .radar import RADAR_FROZEN_STATES
            app = get_application(job_id)
            frozen = app is not None and app["state"] in RADAR_FROZEN_STATES and item["status"] not in {"READY_FOR_CLAUDE", "UNCHANGED"}
            if not frozen:  # e.g. an already-SUBMITTED job keeps its SUBMITTED queue row
                park_queue(job_id, _QUEUE_STATUS_FOR_STATUS[item["status"]], None if item["status"] in {"READY_FOR_CLAUDE", "UNCHANGED"} else f"{item.get('reason')}: {str(item.get('detail') or '')[:300]}")
        except Exception:  # noqa: BLE001 - bookkeeping must not lose the result
            pass
        attempted.append(item)

    counts = {"ready_for_claude": 0, "failed": 0, "ineligible": 0, "needs_confirmation": 0, "skipped_duplicate": 0, "closed": 0}
    for item in attempted:
        counts[_BATCH_BUCKET_FOR_STATUS[item["status"]]] += 1
    assert sum(counts.values()) == len(attempted), "every attempted job must land in exactly one accounting bucket"
    result = {"attempted": len(attempted), **counts, "jobs": attempted}
    ready = [item for item in attempted if item["status"] in {"READY_FOR_CLAUDE", "UNCHANGED"}]
    if ready:
        result["batch_instruction_path"] = str(write_batch_instruction(ready, root=root, submission_authorized=submission_authorized))
    return result


def handoff_batch(*, limit: int = 10, root: Path | None = None, submission_authorized: bool = False) -> dict[str, Any]:
    """Prepare the next `limit` highest-priority queued jobs (see handoff_job_ids)."""
    from .radar import reconcile_queue_with_applications
    reconcile_queue_with_applications()
    rerank_queue()
    selected = select_queued_jobs(limit)
    return handoff_job_ids([row["job_id"] for row in selected], root=root, submission_authorized=submission_authorized,
                           rows={row["job_id"]: row for row in selected})


def write_batch_instruction(ready: list[dict[str, Any]], *, root: Path, submission_authorized: bool) -> Path:
    """One instruction for operating a whole batch with batched human verification."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / "output" / "batches" / stamp / "batch_instruction.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# JOB BEAST BATCH {stamp} -- {len(ready)} application(s)",
        f"submission_authorized: {str(bool(submission_authorized)).lower()}",
        "",
        "Operate these in order. Each job's own claude_browser_prompt.md is authoritative for that job.",
        "",
    ]
    for n, item in enumerate(ready, 1):
        lines.append(f"{n}. {item['company']} / {item['title']} -- job_id {item['job_id']} -- fit {item.get('fit_score')} -- {item.get('prompt_path')}")
    lines += [
        "",
        "## Batch protocol",
        "- One job's failure (layout, navigation, upload, timeout, broken posting, unknown question) never stops the batch:",
        "  record the exact failure in that job's claude_result.json, run import-result, and continue with the next job.",
        "- CAPTCHA / MFA: finish everything else on that application, record READY_FOR_HUMAN_CAPTCHA / READY_FOR_HUMAN_MFA,",
        "  keep the tab open, and continue with the other jobs.",
        "- When every other job has reached a terminal or parked state, send ONE message:",
        "",
        "  HUMAN VERIFICATION BATCH READY",
        "  CAPTCHA: <n. Company / Role> ...",
        "  MFA: ...",
        "  NEEDS INPUT: ...",
        "",
        "- As soon as the user completes one verification, re-verify that application (step 9 of its prompt) and,",
        "  if submission_authorized, submit it immediately -- do not wait for the rest (tokens expire).",
        "- Never mark SUBMITTED without the employer's confirmation. Never submit the same job twice.",
        "- Do not modify Job Beast source code or debug ATS sites during a batch.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def import_result(job_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Idempotently ingest output/<job_id>/claude_result.json and update
    Job Beast's persistent application state. Rejects job_id mismatches and
    stale packet/resume references; re-importing an unchanged result is a
    safe no-op (no duplicate receipts/events/submissions)."""
    root = root or ROOT
    output_dir = root / "output" / job_id
    result_path = output_dir / "claude_result.json"
    if not result_path.exists():
        raise HandoffError("RESULT_NOT_FOUND", str(result_path))
    result = json.loads(result_path.read_text(encoding="utf-8"))

    if result.get("job_id") != job_id:
        raise HandoffError("JOB_ID_MISMATCH", f"result.job_id={result.get('job_id')!r} != {job_id!r}")

    app = get_application(job_id)
    if app is None:
        raise HandoffError("UNKNOWN_APPLICATION", job_id)
    current_version = int(app["handoff_version"] or 0)
    if result.get("packet_version") != current_version:
        raise HandoffError("STALE_PACKET_VERSION", f"result references packet_version={result.get('packet_version')} but current is {current_version}; re-run handoff")

    manifest_path = Path(app["resume_manifest_path"]) if app["resume_manifest_path"] else None
    current_resume_sha = None
    if manifest_path and manifest_path.exists():
        current_resume_sha = json.loads(manifest_path.read_text(encoding="utf-8")).get("resume_sha256")
    if current_resume_sha and result.get("resume_sha256") != current_resume_sha:
        raise HandoffError("STALE_RESUME_SHA256", f"result references resume_sha256={result.get('resume_sha256')} but current is {current_resume_sha}; re-run handoff")

    result_hash = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
    if app["handoff_result_hash"] == result_hash:
        return {"status": "NOOP_ALREADY_IMPORTED", "job_id": job_id, "state": app["state"]}

    status = str(result.get("status") or "")
    submitted = bool(result.get("submitted"))
    confirmed = bool(result.get("confirmation_detected"))

    if status == "SUBMITTED" and submitted and confirmed:
        target = "CLAUDE_SUBMITTED"
    elif status in {"READY_FOR_HUMAN_CAPTCHA", "CAPTCHA"}:
        target = "READY_FOR_HUMAN_CAPTCHA"
    elif status in {"READY_FOR_HUMAN_MFA", "MFA"}:
        target = "READY_FOR_HUMAN_MFA"
    elif status == "SUBMITTED_UNVERIFIED" or (status == "SUBMITTED" and submitted and not confirmed):
        # The submit appeared to go through but no employer confirmation was
        # seen. Never promoted to SUBMITTED and never retried automatically.
        target = "SUBMITTED_UNVERIFIED"
    elif status in {"NEEDS_INPUT", "READY_TO_SUBMIT"}:
        # READY_TO_SUBMIT: fully prepared, submission not authorized yet.
        target = "CLAUDE_NEEDS_INPUT"
    elif status == "INELIGIBLE_HARD_REQUIREMENT":
        target = "INELIGIBLE_HARD_REQUIREMENT"
    elif status in {"FAILED", "SKIPPED", "CLOSED"}:
        target = "CLAUDE_FAILED"
    elif status == "SUBMITTED":
        # Claimed submission without independent confirmation is not trusted.
        target = "CLAUDE_FAILED"
        result = {**result, "failure_reason": (result.get("failure_reason") or "") + " [submitted=true but confirmation_detected was not true; not promoted to SUBMITTED]"}
    else:
        target = "CLAUDE_FAILED"

    if app["state"] != target:
        transition_application(job_id, target, event_type="claude_result_imported", payload=result)
    if target == "INELIGIBLE_HARD_REQUIREMENT":
        # The live posting outranks our scrape: persist its evidence as the
        # job's eligibility verdict so the radar never re-queues it.
        code = result.get("failure_reason") or "live_posting_hard_requirement"
        decision = {
            "status": "INELIGIBLE", "reason": code, "source": "live_posting",
            "hard_requirements": [{"code": code, "status": "INELIGIBLE", "evidence": result.get("live_posting_evidence") or ""}],
        }
        update_job_eligibility(job_id, decision)
        update_queue_eligibility(job_id, "INELIGIBLE")

    write_receipt(output_dir, result)
    if result.get("unresolved_questions"):
        from .apply.receipts import write_unresolved
        write_unresolved(output_dir, result["unresolved_questions"], reason=status or "NEEDS_INPUT")
    record_application_artifact(job_id, "claude_result", str(result_path), sha256=result_hash, metadata={"status": status})

    if target == "CLAUDE_SUBMITTED":
        transition_application(
            job_id, "SUBMITTED", event_type="claude_submission_confirmed",
            payload={"final_url": result.get("final_url"), "confirmation_text": result.get("confirmation_text"), "submitted_at": result.get("submitted_at")},
        )
        update_application(job_id, submitted_at=result.get("submitted_at") or datetime.now(timezone.utc).isoformat(), submission_method="CLAUDE_BROWSER_OPERATOR")

    update_application(job_id, handoff_result_hash=result_hash,
                       last_error=(f"{result.get('failure_class') or ''}: {result.get('failure_reason') or ''}".strip(": ") or None),
                       last_url=result.get("final_url"))
    final_app = get_application(job_id)
    park_queue(job_id, final_app["state"], result.get("failure_class") or result.get("failure_reason"))
    return {"status": "IMPORTED", "job_id": job_id, "state": final_app["state"]}
