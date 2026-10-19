import json
import shutil
from pathlib import Path

import pytest

from jobbeast.apply.answers import AnswerVault
from jobbeast.handoff import (
    HandoffError, _split_answers, handoff_batch, import_result, prepare_handoff,
)
from jobbeast.store import connect, get_application, has_verified_submission, transition_application

REAL_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fake_root_with_templates(tmp_path):
    # build_application()'s compiled .tex references ../../templates/jakes_resume.tex
    # relative to output/<job_id>/, so a fake root needs the same template layout
    # as the real repo for an end-to-end compile.
    shutil.copytree(REAL_ROOT / "templates", tmp_path / "templates")


GENERIC_DESCRIPTION = (
    "We are hiring a Software Engineering Intern for Summer 2027. You will work with "
    "Python, SQL, REST APIs, and distributed systems. Must be authorized to work in the "
    "United States; we are unable to provide visa sponsorship for this role. "
) * 3


def _insert_job(job_id: str, *, description: str = GENERIC_DESCRIPTION, company: str = "Acme", title: str = "Software Engineer Intern", hydrated: bool = True) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO jobs(id,fingerprint,source,company,title,url,locations_json,description,date_posted,retrieved_at,active,terms_json,metadata_json,
               canonical_url,official_url,ats_provider,provider_job_id,jd_hash,hydration_status,source_date_posted)
               VALUES(?,?,?,?,?,?,?,?,?,?,1,'[]','{}',?,?,?,?,?,?,?)""",
            (
                job_id, f"fp-{job_id}", "test", company, title, f"https://example.test/{job_id}",
                "[]", description, "2026-10-12T00:00:00Z", "2026-10-12T00:00:00Z",
                f"https://boards.greenhouse.io/{company.lower()}/jobs/{job_id}",
                f"https://boards.greenhouse.io/{company.lower()}/jobs/{job_id}",
                "greenhouse", job_id, "jd-hash-value",
                "HYDRATED" if hydrated else "INCOMPLETE", "2026-10-12T00:00:00Z",
            ),
        )


def test_prepare_handoff_happy_path_produces_all_artifacts_and_transitions_to_ready_for_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-1")
    result = prepare_handoff("job-1", root=tmp_path)

    assert result["status"] == "READY_FOR_CLAUDE"
    assert result["packet_version"] == 1
    output_dir = tmp_path / "output" / "job-1"
    packet_path = Path(result["packet_path"])
    assert packet_path == output_dir / "application_packet.json"
    for name in ("application_packet.json", "application_context.md", "claude_browser_prompt.md", "claude_result.json", "resume_final.pdf", "operator_instruction.md"):
        assert (output_dir / name).exists(), name
    assert result["operator_instruction_path"] == str(output_dir / "operator_instruction.md")
    instruction = (output_dir / "operator_instruction.md").read_text()
    assert "job-1" in instruction and "greenhouse.io" in instruction

    packet = json.loads(packet_path.read_text())
    assert packet["job_id"] == "job-1"
    assert packet["submission_authorized"] is False
    assert packet["canonical_application_url"].startswith("https://boards.greenhouse.io/")
    assert packet["resume_sha256"] == result["resume_sha256"]
    assert packet["packet_version"] == 1
    assert packet["context_sha256"]

    result_template = json.loads((output_dir / "claude_result.json").read_text())
    assert result_template["job_id"] == "job-1"
    assert result_template["packet_version"] == 1
    assert result_template["resume_sha256"] == result["resume_sha256"]
    assert result_template["status"] is None

    assert get_application("job-1")["state"] == "READY_FOR_CLAUDE"


def test_prepare_handoff_never_leaks_eeo_or_demographic_data_into_the_packet(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-eeo")
    result = prepare_handoff("job-eeo", root=tmp_path)
    packet_text = Path(result["packet_path"]).read_text()
    context_text = Path(result["context_path"]).read_text()
    for forbidden in ("demographics.", "voluntary_eeo.", "hispanic_latino", "veteran_status", "disability_status", "voluntary_demographics_policy"):
        assert forbidden not in packet_text, f"{forbidden} leaked into application_packet.json"
        assert forbidden not in context_text, f"{forbidden} leaked into application_context.md"


def test_split_answers_always_carries_confirmed_legal_answers_and_excludes_eeo():
    vault = AnswerVault(path=Path("test.yaml"), data={
        "identity": {"email": "a@example.com"},
        "work_preferences": {"work_authorization": True, "sponsorship_now": False, "relocation": True},
        "demographics": {"gender": "DECLINE"},
        "compensation": {"expected_salary": "REQUIRES_USER_INPUT"},
    })
    # Forms ask work-authorization/sponsorship questions whether or not the JD
    # mentions them, so user-confirmed legal answers are always carried (their
    # reuse is limited to same-meaning questions by the operator instructions).
    no_signal = _split_answers(vault, description="We are a great team building cool products.")
    assert no_signal["legal_sensitive_answers"]["work_preferences.work_authorization"] is True
    assert not any(k.startswith("demographics.") for k in no_signal["verified_application_answers"])
    assert not any(item["question"].startswith("demographics.") for item in no_signal["unknown_answers"])

    signaled = _split_answers(vault, description="You must be authorized to work in the United States.")
    assert signaled["legal_sensitive_answers"]["work_preferences.work_authorization"] is True
    assert signaled["legal_sensitive_answers"]["work_preferences.sponsorship_now"] is False

    unknown_by_question = {item["question"]: item for item in signaled["unknown_answers"]}
    assert unknown_by_question["compensation.expected_salary"]["recommended_action"] == "DRAFT_FROM_EVIDENCE"
    assert "demographics.gender" not in unknown_by_question


def test_prepare_handoff_allows_prep_when_stored_jd_is_sufficient_despite_incomplete_hydration_status(monkeypatch, tmp_path):
    # hydration_status reflects only the most recent fetch attempt; the stored
    # description (preserved by update_job_hydration's max-length CASE) can
    # already be long enough to tailor truthfully even if the latest live
    # scrape was JS-blocked. This must NOT be treated as a URL problem.
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-sufficient-jd", hydrated=False)  # GENERIC_DESCRIPTION is already >=300 chars
    result = prepare_handoff("job-sufficient-jd", root=tmp_path)
    assert result["status"] == "READY_FOR_CLAUDE"
    packet = json.loads(Path(result["packet_path"]).read_text())
    assert packet["jd_source_verified"] is False
    assert packet["jd_hydration_state"] == "JS_RENDERED_LIVE_VERIFICATION_REQUIRED"
    prompt_text = Path(result["prompt_path"]).read_text()
    context_text = Path(result["context_path"]).read_text()
    assert "JS_RENDERED_LIVE_VERIFICATION_REQUIRED" in context_text
    assert "did not come from a confirmed read of the employer's live page" in context_text
    assert "cross-check the" in prompt_text and "live requirements" in prompt_text


def test_prepare_handoff_reports_needs_hydration_not_url_invalid_when_jd_is_too_thin(monkeypatch, tmp_path):
    import jobbeast.handoff as handoff_module

    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-thin", description="Short stub.", hydrated=False)
    # Simulate a real re-fetch attempt that still comes back too thin (e.g.
    # the posting requires JS rendering this pass's static hydration can't
    # follow) without making a real network call in this test.
    monkeypatch.setattr(handoff_module, "hydrate_job", lambda job_id, force=False: {"job_id": job_id, "status": "INCOMPLETE"})
    with pytest.raises(HandoffError) as exc:
        prepare_handoff("job-thin", root=tmp_path)
    assert exc.value.reason == "NEEDS_HYDRATION"


def test_prepare_handoff_aborts_cleanly_when_ineligible(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-ineligible", description="Must be a U.S. citizen. This role requires U.S. person status and is required for export control eligibility purposes under ITAR.")
    with pytest.raises(HandoffError) as exc:
        prepare_handoff("job-ineligible", root=tmp_path)
    assert exc.value.reason == "INELIGIBLE"
    assert not (tmp_path / "output" / "job-ineligible" / "application_packet.json").exists()


def test_prepare_handoff_aborts_when_already_submitted(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-dup")
    prepare_handoff("job-dup", root=tmp_path)
    transition_application("job-dup", "READY_FOR_CLAUDE")  # already there; no-op re-set
    transition_application("job-dup", "CLAUDE_SUBMITTED")
    transition_application("job-dup", "SUBMITTED")
    assert has_verified_submission("job-dup")
    with pytest.raises(HandoffError) as exc:
        prepare_handoff("job-dup", root=tmp_path)
    assert exc.value.reason == "ALREADY_SUBMITTED"


def test_prepare_handoff_skips_rebuild_when_unchanged_and_rebuilds_on_force(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-rebuild")
    first = prepare_handoff("job-rebuild", root=tmp_path)
    assert first["packet_version"] == 1

    second = prepare_handoff("job-rebuild", root=tmp_path)
    assert second["status"] == "UNCHANGED"
    assert second["packet_version"] == 1

    third = prepare_handoff("job-rebuild", root=tmp_path, force=True)
    assert third["status"] == "READY_FOR_CLAUDE"
    assert third["packet_version"] == 2


def test_handoff_batch_continues_after_one_job_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-ok-1", company="GoodCo")
    _insert_job("job-bad", description="Must be a U.S. citizen. This role requires U.S. person status and is required for export control eligibility under ITAR.", company="BadCo")
    _insert_job("job-ok-2", company="GreatCo")
    for job_id, status, lane in (("job-ok-1", "QUEUED", "BACKLOG"), ("job-bad", "QUEUED", "BACKLOG"), ("job-ok-2", "QUEUED", "BACKLOG")):
        with connect() as con:
            con.execute(
                "INSERT INTO application_queue(job_id,status,lane,queue_priority,fit_score,freshness_score,eligibility_status) VALUES(?,?,?,1,1,0,'ELIGIBLE')",
                (job_id, status, lane),
            )
    result = handoff_batch(limit=10, root=tmp_path)
    assert result["attempted"] == 3
    assert result["ready_for_claude"] == 2
    assert result["ineligible"] == 1
    assert result["failed"] == 0
    assert result["attempted"] == sum(result[k] for k in ("ready_for_claude", "failed", "ineligible", "needs_confirmation", "skipped_duplicate", "closed"))
    ineligible = [j for j in result["jobs"] if j["status"] == "INELIGIBLE"][0]
    assert ineligible["job_id"] == "job-bad"
    assert ineligible["reason"] == "INELIGIBLE"


def test_handoff_batch_accounting_covers_every_terminal_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-ready", company="ReadyCo")
    _insert_job("job-ineligible", description="Must be a U.S. citizen. This role requires U.S. person status and is required for export control eligibility under ITAR.", company="IneligibleCo")
    _insert_job("job-needs-confirmation", description="Are you legally authorized to work in the United Kingdom?", company="UKCo")
    _insert_job("job-thin-jd", description="x", company="ThinCo", hydrated=False)
    jobs = ["job-ready", "job-ineligible", "job-needs-confirmation", "job-thin-jd", "job-closed", "job-duplicate"]
    _insert_job("job-closed", company="ClosedCo")
    # has_verified_submission() dedupes by fingerprint OR (company, title) --
    # simulate a second listing of an already-submitted role under a
    # different job_id, which is the realistic SKIPPED_DUPLICATE case.
    _insert_job("job-duplicate-original", company="DupCo")
    _insert_job("job-duplicate", company="DupCo")
    with connect() as con:
        con.execute("UPDATE jobs SET active=0 WHERE id='job-closed'")
    for job_id in jobs:
        with connect() as con:
            con.execute(
                "INSERT INTO application_queue(job_id,status,lane,queue_priority,fit_score,freshness_score,eligibility_status) VALUES(?,?,?,1,1,0,'ELIGIBLE')",
                (job_id, "QUEUED", "BACKLOG"),
            )
    prepare_handoff("job-duplicate-original", root=tmp_path)
    transition_application("job-duplicate-original", "CLAUDE_SUBMITTED")
    transition_application("job-duplicate-original", "SUBMITTED")
    assert has_verified_submission("job-duplicate")

    # handoff_job_ids() (explicit job list, e.g. the acceptance test) attempts all six.
    from jobbeast.handoff import handoff_job_ids
    result = handoff_job_ids(jobs, root=tmp_path)
    assert result["attempted"] == len(jobs)
    assert result["attempted"] == sum(result[k] for k in ("ready_for_claude", "failed", "ineligible", "needs_confirmation", "skipped_duplicate", "closed"))
    statuses = {j["job_id"]: j["status"] for j in result["jobs"]}
    assert statuses["job-ready"] == "READY_FOR_CLAUDE"
    assert statuses["job-ineligible"] == "INELIGIBLE"
    assert statuses["job-needs-confirmation"] == "NEEDS_CONFIRMATION"
    assert statuses["job-thin-jd"] == "NEEDS_HYDRATION"  # counted in the failed bucket
    assert statuses["job-closed"] == "CLOSED"
    assert statuses["job-duplicate"] == "SKIPPED_DUPLICATE"


def test_handoff_batch_never_selects_a_closed_listing(monkeypatch, tmp_path):
    from jobbeast.store import queue_row
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-closed", company="ClosedCo")
    _queue("job-closed")
    with connect() as con:
        con.execute("UPDATE jobs SET active=0 WHERE id='job-closed'")
    assert handoff_batch(limit=10, root=tmp_path)["attempted"] == 0
    assert queue_row("job-closed")["status"] == "CLOSED"


def test_handoff_batch_does_not_reselect_an_already_ready_job(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-first", company="FirstCo")
    _insert_job("job-second", company="SecondCo")
    for job_id in ("job-first", "job-second"):
        with connect() as con:
            con.execute(
                "INSERT INTO application_queue(job_id,status,lane,queue_priority,fit_score,freshness_score,eligibility_status) VALUES(?,?,'BACKLOG',1,1,0,'ELIGIBLE')",
                (job_id, "QUEUED"),
            )
    first_round = handoff_batch(limit=1, root=tmp_path)
    assert first_round["jobs"][0]["job_id"] == "job-first"
    assert first_round["jobs"][0]["status"] == "READY_FOR_CLAUDE"

    second_round = handoff_batch(limit=1, root=tmp_path)
    assert second_round["jobs"][0]["job_id"] == "job-second"
    assert second_round["jobs"][0]["status"] == "READY_FOR_CLAUDE"


def test_import_result_rejects_job_id_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-mismatch")
    prepare_handoff("job-mismatch", root=tmp_path)
    result_path = tmp_path / "output" / "job-mismatch" / "claude_result.json"
    payload = json.loads(result_path.read_text())
    payload["job_id"] = "some-other-job"
    result_path.write_text(json.dumps(payload))
    with pytest.raises(HandoffError) as exc:
        import_result("job-mismatch", root=tmp_path)
    assert exc.value.reason == "JOB_ID_MISMATCH"


def test_import_result_rejects_stale_packet_version(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-stale")
    prepare_handoff("job-stale", root=tmp_path)
    prepare_handoff("job-stale", root=tmp_path, force=True)  # bumps to packet_version 2
    result_path = tmp_path / "output" / "job-stale" / "claude_result.json"
    payload = json.loads(result_path.read_text())
    payload.update(packet_version=1, status="SUBMITTED", submitted=True, confirmation_detected=True)
    result_path.write_text(json.dumps(payload))
    with pytest.raises(HandoffError) as exc:
        import_result("job-stale", root=tmp_path)
    assert exc.value.reason == "STALE_PACKET_VERSION"


def test_import_result_promotes_to_submitted_only_with_independent_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-submit")
    handoff = prepare_handoff("job-submit", root=tmp_path)
    result_path = tmp_path / "output" / "job-submit" / "claude_result.json"
    payload = json.loads(result_path.read_text())
    payload.update(status="SUBMITTED", submitted=True, confirmation_detected=False, final_url="https://example.test/confirm")
    result_path.write_text(json.dumps(payload))
    outcome = import_result("job-submit", root=tmp_path)
    assert outcome["state"] == "SUBMITTED_UNVERIFIED"  # claimed submit without confirmation is not trusted

    payload.update(confirmation_detected=True, submitted_at="2026-10-12T01:00:00Z")
    result_path.write_text(json.dumps(payload))
    outcome = import_result("job-submit", root=tmp_path)
    assert outcome["state"] == "SUBMITTED"
    assert has_verified_submission("job-submit")


def test_import_result_is_idempotent_on_duplicate_import(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-idempotent")
    prepare_handoff("job-idempotent", root=tmp_path)
    result_path = tmp_path / "output" / "job-idempotent" / "claude_result.json"
    payload = json.loads(result_path.read_text())
    payload.update(status="NEEDS_INPUT", submitted=False, confirmation_detected=False)
    result_path.write_text(json.dumps(payload))

    first = import_result("job-idempotent", root=tmp_path)
    assert first["status"] == "IMPORTED"
    with connect() as con:
        events_after_first = len(con.execute("SELECT 1 FROM application_events WHERE job_id='job-idempotent'").fetchall())

    second = import_result("job-idempotent", root=tmp_path)
    assert second["status"] == "NOOP_ALREADY_IMPORTED"
    with connect() as con:
        events_after_second = len(con.execute("SELECT 1 FROM application_events WHERE job_id='job-idempotent'").fetchall())
    assert events_after_first == events_after_second


EA_MASTERS_DESCRIPTION = (
    "Data & AI Engineering Intern (Master's Level) - Summer 2027 - 216238 - Electronic Arts\n"
    "Build and evaluate LLM or agent workflows using prompts, RAG, APIs, tool calling, and structured outputs. "
    "Develop reliable Python and SQL components, tools, and pipelines for data ingestion and validation.\n"
    "Qualifications\n"
    "Currently pursuing a graduate degree in Computer Science, Data Engineering, Data Science, or a related technical field.\n"
) * 2


def test_ea_masters_level_job_already_ready_for_claude_is_demoted(monkeypatch, tmp_path):
    # Real regression: Electronic Arts 1673c0927276724c reached READY_FOR_CLAUDE
    # under the listing title "Data and AI Engineer Intern" although the live
    # posting is "(Master's Level)" and requires graduate enrollment.
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-ea", company="Electronic Arts", title="Data and AI Engineer Intern")
    assert prepare_handoff("job-ea", root=tmp_path)["status"] == "READY_FOR_CLAUDE"

    with connect() as con:  # re-hydration brings in the live posting text
        con.execute("UPDATE jobs SET description=? WHERE id='job-ea'", (EA_MASTERS_DESCRIPTION,))
    with pytest.raises(HandoffError) as exc:
        prepare_handoff("job-ea", root=tmp_path, force=True)
    assert exc.value.reason == "INELIGIBLE"
    assert get_application("job-ea")["state"] == "INELIGIBLE_HARD_REQUIREMENT"
    with connect() as con:
        eligibility = json.loads(con.execute("SELECT eligibility_json FROM jobs WHERE id='job-ea'").fetchone()[0])
    assert eligibility["reason"] == "graduate_degree_required"


def test_import_result_records_live_posting_hard_ineligibility(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-live-inelig")
    prepare_handoff("job-live-inelig", root=tmp_path)
    result_path = tmp_path / "output" / "job-live-inelig" / "claude_result.json"
    payload = json.loads(result_path.read_text())
    assert "live_posting_evidence" in payload
    payload.update(
        status="INELIGIBLE_HARD_REQUIREMENT", submitted=False, failure_reason="graduate_degree_required",
        live_posting_evidence={"live_title": "Data & AI Engineering Intern (Master's Level) - Summer 2027"},
    )
    result_path.write_text(json.dumps(payload))

    outcome = import_result("job-live-inelig", root=tmp_path)
    assert outcome["state"] == "INELIGIBLE_HARD_REQUIREMENT"
    with connect() as con:
        job = con.execute("SELECT eligibility_status, eligibility_json FROM jobs WHERE id='job-live-inelig'").fetchone()
    assert job["eligibility_status"] == "INELIGIBLE"
    decision = json.loads(job["eligibility_json"])
    assert decision["reason"] == "graduate_degree_required" and decision["source"] == "live_posting"
    assert "Master's Level" in json.dumps(decision["hard_requirements"][0]["evidence"])


def _queue(job_id, fit=80.0):
    from jobbeast.store import upsert_application_queue
    upsert_application_queue(job_id, status="QUEUED", lane="BACKLOG", queue_priority=fit, fit_score=fit, freshness_score=0, eligibility_status="ELIGIBLE")


def test_failed_handoff_parks_the_job_so_later_batches_never_reselect_it(monkeypatch, tmp_path):
    from jobbeast.store import queue_row
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-thin", description="Too short.", hydrated=False)
    _queue("job-thin")
    monkeypatch.setattr("jobbeast.handoff.hydrate_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("JS-only page")))
    first = handoff_batch(limit=10, root=tmp_path)
    assert [j["status"] for j in first["jobs"]] == ["NEEDS_HYDRATION"]
    assert queue_row("job-thin")["status"] == "NEEDS_HYDRATION"
    assert handoff_batch(limit=10, root=tmp_path)["attempted"] == 0


def test_dead_posting_is_url_invalid(monkeypatch, tmp_path):
    import requests
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-gone", description="Too short.", hydrated=False)
    response = requests.Response()
    response.status_code = 404
    monkeypatch.setattr("jobbeast.handoff.hydrate_job", lambda *a, **k: (_ for _ in ()).throw(requests.HTTPError(response=response)))
    with pytest.raises(HandoffError) as exc:
        prepare_handoff("job-gone", root=tmp_path)
    assert exc.value.reason == "URL_INVALID"


def test_submission_authorization_flows_into_packet_and_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-a")
    held = prepare_handoff("job-a", root=tmp_path)
    assert "Do not click Submit" in Path(held["prompt_path"]).read_text()
    authorized = prepare_handoff("job-a", root=tmp_path, submission_authorized=True)
    assert authorized["status"] == "READY_FOR_CLAUDE" and authorized["packet_version"] == 2  # authorization change rebuilds
    assert json.loads(Path(authorized["packet_path"]).read_text())["submission_authorized"] is True
    prompt = Path(authorized["prompt_path"]).read_text()
    assert "click Submit without asking" in prompt and "READY_FOR_HUMAN_CAPTCHA" in prompt
    assert "hidden DOM text alone is not proof" in prompt


def test_handoff_batch_writes_one_consolidated_batch_instruction(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    for jid in ("job-b1", "job-b2"):
        _insert_job(jid, company=f"Co{jid[-1]}")
        _queue(jid)
    result = handoff_batch(limit=10, root=tmp_path)
    text = Path(result["batch_instruction_path"]).read_text()
    assert "HUMAN VERIFICATION BATCH READY" in text and "job-b1" in text and "job-b2" in text


def test_import_result_parks_captcha_and_mfa_for_the_human(monkeypatch, tmp_path):
    from jobbeast.store import queue_row
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    for jid, status in (("job-cap", "READY_FOR_HUMAN_CAPTCHA"), ("job-mfa", "READY_FOR_HUMAN_MFA")):
        _insert_job(jid, company=jid)
        _queue(jid)
        ready = prepare_handoff(jid, root=tmp_path)
        result_path = Path(ready["result_template_path"])
        payload = json.loads(result_path.read_text())
        payload.update(status=status, submitted=False, confirmation_detected=False)
        result_path.write_text(json.dumps(payload))
        assert import_result(jid, root=tmp_path)["state"] == status
        assert queue_row(jid)["status"] == status
    assert handoff_batch(limit=10, root=tmp_path)["attempted"] == 0  # parked jobs are not reselected


def test_resume_quality_gate_catches_duplicates_leaks_collapsed_skills_and_wrong_job():
    from jobbeast.handoff import resume_quality_errors
    from jobbeast.profile import load_profile
    profile = load_profile()
    head = f"{profile['name']}\n{profile['contact']['email']} {profile['contact']['phone']}\n"
    good_cov = {"job": {"company": "Acme", "title": "SWE Intern"}, "included_skills": {"Languages": "Python, Java, SQL", "Tools": "Git, Docker, Linux"}}
    assert resume_quality_errors(head + "• Built X\n• Built Y\n", coverage=good_cov, company="Acme", title="SWE Intern") == []
    errors = resume_quality_errors(
        head + "• Built X\n• Built X\nPronouns: he/him  F-1 visa\n",
        coverage={"job": {"company": "Other", "title": "SWE Intern"}, "included_skills": {"Languages": "Python, SQL"}},
        company="Acme", title="SWE Intern",
    )
    joined = " | ".join(errors)
    assert "duplicate bullets" in joined and "private/EEO" in joined and "collapsed" in joined and "tailored for" in joined
    assert resume_quality_errors("• race conditions in a lock manager\n" + head, coverage=good_cov, company="Acme", title="SWE Intern") == []


def test_import_unconfirmed_submission_is_submitted_unverified_and_blocks_duplicates(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    _insert_job("job-unv")
    ready = prepare_handoff("job-unv", root=tmp_path)
    path = Path(ready["result_template_path"])
    payload = json.loads(path.read_text())
    payload.update(status="SUBMITTED_UNVERIFIED", submitted=True, confirmation_detected=False)
    path.write_text(json.dumps(payload))
    assert import_result("job-unv", root=tmp_path)["state"] == "SUBMITTED_UNVERIFIED"
    assert has_verified_submission("job-unv")  # duplicate guard
    with pytest.raises(HandoffError) as exc:
        prepare_handoff("job-unv", root=tmp_path)
    assert exc.value.reason == "ALREADY_SUBMITTED"
