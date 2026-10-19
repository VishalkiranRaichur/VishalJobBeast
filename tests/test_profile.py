"""The public repo must run out of the box against the fictional example profile."""
from jobbeast import profile
from jobbeast.evidence import validate_evidence_bank


def test_default_profile_is_the_fictional_example():
    assert profile.PROFILE_DIR.name == "example"
    data = profile.load_profile()
    assert data["name"] == "Alex Rivera"
    assert data["contact"]["email"].endswith("@example.com")


def test_resume_terms_come_from_the_profile():
    assert profile.required_resume_terms()[0] == "Alex Rivera"
    # Unverified projects can never reach a resume.
    assert "Unverified Idea" in profile.forbidden_resume_terms()


def test_example_evidence_bank_is_valid():
    assert validate_evidence_bank() == []
