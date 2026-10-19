from pathlib import Path

from jobbeast.apply.answers import AnswerVault
from jobbeast.apply.detector import detect_provider
from jobbeast.apply.questions import classify_question
from jobbeast.apply.state import ApplicationState, can_transition
from jobbeast.store import connect, ensure_application, get_application, transition_application, update_job_detection
from jobbeast.dashboard import write_dashboard


def test_provider_detection_uses_hostname_boundaries():
    assert detect_provider("https://boards.greenhouse.io/acme/jobs/1").provider == "greenhouse"
    assert detect_provider("https://notgreenhouse.io/job").provider == "unknown"
    assert detect_provider("https://jobs.lever.co/acme/1").official_host_verified


def test_provider_fingerprint_recognizes_supported_hosts():
    cases = {
        "greenhouse": "https://boards.greenhouse.io/acme/jobs/123",
        "lever": "https://jobs.lever.co/acme/123",
        "ashby": "https://jobs.ashbyhq.com/acme/123",
        "workday": "https://acme.wd1.myworkdayjobs.com/en-US/jobs/123",
        "icims": "https://careers.acme.icims.com/jobs/123/job",
        "smartrecruiters": "https://jobs.smartrecruiters.com/acme/123",
        "phenom": "https://acme.phenompeople.com/us/en/job/123",
        "eightfold": "https://acme.eightfold.ai/careers/job/123",
        "successfactors": "https://jobs.successfactors.com/acme/job/123",
        "oracle_cloud": "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123",
        "taleo": "https://acme.taleo.net/careersection/jobdetail.ftl?job=123",
    }
    for provider, url in cases.items():
        result = detect_provider(url)
        assert result.provider == provider
        assert result.confidence >= 0.8
        assert result.provider_host
        assert result.signals


def test_unknown_provider_keeps_fingerprint_diagnostics():
    result = detect_provider("https://careers.example.test/job/42", html='<html><title>Apply</title><form action="/apply"><input required name="first_name"></form></html>')
    assert result.provider == "unknown"
    assert "generic application control/form detected" in result.signals
    assert result.page_title == "Apply"
    assert result.form_actions == ("/apply",)


def test_hudson_river_trading_greenhouse_job_id_is_fingerprinted():
    result = detect_provider("https://www.hudsonrivertrading.com/careers/job/?gh_jid=8257369", html='<div id="grnhse_app_new"></div>')
    assert result.provider == "greenhouse"
    assert result.provider_job_id == "8257369"


def test_ea_avature_marker_is_not_reported_as_unknown():
    result = detect_provider("https://jobs.ea.com/en_US/careers/JobDetail/Software-Engineer-Intern/216251", html='<meta name="avature.portal.id" content="4">')
    assert result.provider == "avature"
    assert any("avature" in signal.lower() for signal in result.signals)


def test_provider_fingerprint_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    with connect() as con:
        con.execute("INSERT INTO jobs(id,fingerprint,source,company,title,url,locations_json,terms_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", ("j-fp", "fp", "test", "Acme", "Intern", "https://example.test/job/42", "[]", "[]", "{}"))
    detection = detect_provider("https://careers.acme.icims.com/jobs/42/job")
    update_job_detection("j-fp", detection.as_dict())
    with connect() as con:
        row = con.execute("SELECT ats_provider,ats_provider_confidence,ats_provider_host,provider_job_id,ats_provider_evidence,fingerprint_json FROM jobs WHERE id='j-fp'").fetchone()
    assert row["ats_provider"] == "icims"
    assert row["ats_provider_confidence"] >= 0.8
    assert row["ats_provider_host"] == "careers.acme.icims.com"
    assert row["provider_job_id"] == "42"
    assert "hostname contains recognized icims host" in row["ats_provider_evidence"]
    assert '"provider": "icims"' in row["fingerprint_json"]


def test_question_classifier_keeps_legal_questions_distinct():
    assert classify_question("Will you require sponsorship now or in the future?", True).category == "SPONSORSHIP"
    assert classify_question("What Python projects are you most proud of?").category in {"SKILL", "FREE_RESPONSE"}


def test_answer_vault_marks_sensitive_placeholders(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("work_preferences:\n  work_authorization: REQUIRES_USER_INPUT\nidentity:\n  email: a@example.com\n", encoding="utf-8")
    vault = AnswerVault.load(path)
    assert vault.known("identity.email").value == "a@example.com"
    assert vault.known("work_preferences.work_authorization") is None
    assert "work_preferences.work_authorization" in vault.unknowns()


def test_application_state_transition_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    with connect() as con:
        con.execute("INSERT INTO jobs(id,fingerprint,source,company,title,url,locations_json,terms_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", ("j1", "f1", "test", "Acme", "Intern", "https://example.com", "[]", "[]", "{}"))
    ensure_application("j1")
    transition_application("j1", ApplicationState.RESUME_READY.value)
    transition_application("j1", ApplicationState.READY_TO_APPLY.value)
    assert get_application("j1")["state"] == "READY_TO_APPLY"
    assert can_transition("READY_TO_APPLY", "APPLYING")


def test_claude_handoff_states_fork_from_ready_to_apply_without_disturbing_legacy_path():
    assert can_transition("READY_TO_APPLY", "READY_FOR_CLAUDE")
    assert can_transition("READY_TO_APPLY", "APPLYING")
    assert can_transition("READY_FOR_CLAUDE", "CLAUDE_RUNNING")
    # Claude runs out-of-band; import-result can jump straight from
    # READY_FOR_CLAUDE to a terminal CLAUDE_* state in one shot.
    assert can_transition("READY_FOR_CLAUDE", "CLAUDE_SUBMITTED")
    assert can_transition("READY_FOR_CLAUDE", "CLAUDE_NEEDS_INPUT")
    assert can_transition("READY_FOR_CLAUDE", "CLAUDE_FAILED")
    assert can_transition("CLAUDE_RUNNING", "CLAUDE_NEEDS_INPUT")
    assert can_transition("CLAUDE_RUNNING", "CLAUDE_SUBMITTED")
    assert can_transition("CLAUDE_RUNNING", "CLAUDE_FAILED")
    assert can_transition("CLAUDE_NEEDS_INPUT", "CLAUDE_RUNNING")
    assert can_transition("CLAUDE_NEEDS_INPUT", "CLAUDE_SUBMITTED")
    assert can_transition("CLAUDE_SUBMITTED", "SUBMITTED")
    assert can_transition("CLAUDE_FAILED", "READY_FOR_CLAUDE")
    assert not can_transition("CLAUDE_SUBMITTED", "APPLYING")
    assert not can_transition("READY_FOR_CLAUDE", "SUBMITTED")


def test_claude_handoff_states_do_not_bypass_confirmation():
    # A bare self-report (CLAUDE_SUBMITTED) is not the canonical SUBMITTED state;
    # only import-result's independent confirmation check promotes it. Both
    # edges exist in the DAG, but the promotion itself is gated in import-result,
    # not here -- this just confirms the state machine permits the handoff.
    from jobbeast.apply.state import TRANSITIONS
    assert TRANSITIONS["CLAUDE_SUBMITTED"] == {"SUBMITTED", "CLAUDE_FAILED"}


def test_dashboard_contains_application_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    with connect() as con:
        con.execute("INSERT INTO jobs(id,fingerprint,source,company,title,url,locations_json,terms_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", ("j1", "f1", "test", "Acme", "Intern", "https://example.com", "[]", "[]", "{}"))
    ensure_application("j1")
    path = write_dashboard(tmp_path / "dashboard.html")
    text = path.read_text(encoding="utf-8")
    assert "Acme" in text
    assert "Intern" in text
