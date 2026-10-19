import pytest
from jobbeast.eligibility import classify_hard_eligibility


def test_us_person_export_requirement_is_hard_ineligible():
    decision = classify_hard_eligibility(
        {"description": "U.S. Citizenship, Lawful Permanent Residency, or Refugee/Asylee Status Required. Subject to export regulations."},
        {"work_preferences": {"sponsorship_now": False, "sponsorship_future": False}},
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "export_control_us_person_requirement"


def test_work_authorization_is_not_export_control_eligibility():
    decision = classify_hard_eligibility(
        {"description": "Applicants must be legally authorized to work in the United States."},
        {"work_preferences": {"sponsorship_now": False, "sponsorship_future": False}},
    )
    assert decision["status"] == "ELIGIBLE"


def test_graduate_program_requirement_is_hard_ineligible_for_undergraduate_profile():
    decision = classify_hard_eligibility(
        {"description": "You have: Enrolled in a graduate program (Master's or PhD) in Computer Science."},
        {"education": {"degree": "Bachelor of Science"}, "work_preferences": {}},
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "graduate_degree_required"


def test_high_school_only_requirement_is_hard_ineligible_for_university_profile():
    decision = classify_hard_eligibility(
        {"description": "This internship is for high school students. Minimum requirement: currently enrolled in high school."},
        {"education": {"degree": "Bachelor of Science"}, "work_preferences": {}},
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "high_school_requirement"


def test_master_or_phd_wording_is_hard_ineligible_for_undergraduate_profile():
    decision = classify_hard_eligibility(
        {"description": "Currently pursuing a master's or PhD degree; this co-op requires graduate enrollment."},
        {"education": {"degree": "Bachelor of Science"}, "work_preferences": {}},
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "graduate_degree_required"


def test_progressing_towards_graduate_degree_is_hard_ineligible_for_undergraduate_profile():
    # Real regression: this exact "You have:" phrasing (Waymo, job 751fed5d10df8600)
    # previously slipped past the regex because it only matched enrolled/pursuing,
    # not "progressing towards".
    decision = classify_hard_eligibility(
        {"title": "Intern", "description": "You have: Progressing towards a Masters, or PhD Degree in Computer Science or related technical field."},
        {"education": {"degree": "Bachelor of Science"}, "work_preferences": {}},
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "graduate_degree_required"


def test_graduate_degree_named_only_in_title_is_hard_ineligible_for_undergraduate_profile():
    # Real regression: Lyft job 99e031ab39bc1fad hydrated to a near-empty
    # description, but the title alone ("PhD Machine Learning Software
    # Engineer Intern") already carries the hard requirement.
    decision = classify_hard_eligibility(
        {"title": "PhD Machine Learning Software Engineer Intern (Summer 2027)", "description": "Lyft PhD Machine Learning Software Engineer Intern (Summer 2027)"},
        {"education": {"degree": "Bachelor of Science"}, "work_preferences": {}},
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "graduate_degree_required"


def test_non_us_work_authorization_needs_confirmation():
    decision = classify_hard_eligibility(
        {"description": "Are you legally authorized to work in the United Kingdom?"},
        {"work_preferences": {"sponsorship_now": False, "sponsorship_future": False}},
    )
    assert decision["status"] == "NEEDS_CONFIRMATION"
    assert decision["reason"] == "non_us_work_authorization_needs_confirmation"


UNDERGRAD = {"education": {"degree": "Bachelor of Science"}, "work_preferences": {"sponsorship_now": False, "sponsorship_future": False}}


def test_ea_masters_level_posting_is_hard_ineligible_for_undergraduate_profile():
    # Real regression: Electronic Arts job 1673c0927276724c (Role ID 216238) was
    # stored under the listing title "Data and AI Engineer Intern" and reached
    # READY_FOR_CLAUDE. The live posting title is "(Master's Level)" and the
    # qualifications say "Currently pursuing a graduate degree" -- neither was
    # matched. The body's undergrad sentence must not rescue it.
    decision = classify_hard_eligibility(
        {
            "title": "Data and AI Engineer Intern",
            "description": (
                "Data & AI Engineering Intern (Master's Level) - Summer 2027 - 216238 - Electronic Arts\n"
                "Qualifications\n"
                "Currently pursuing a graduate degree in Computer Science, Data Engineering, Data Science, or a related technical field.\n"
                "At this time, we are considering Undergrad students who will be enrolled in an accredited degree program "
                "in the summer of 2027, slated to graduate no earlier than December 2027. Visa sponsorship is not available for this position."
            ),
        },
        UNDERGRAD,
    )
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "graduate_degree_required"
    assert "Master's Level" in decision["hard_requirements"][0]["evidence"]


@pytest.mark.parametrize("text", [
    "Software Engineer Intern (Master's Level)",
    "Open to Master's students in Computer Science.",
    "This role is for PhD students.",
    "Candidates must be doctoral students in Machine Learning.",
    "Currently pursuing a graduate degree in Data Science.",
    "Requirements: currently enrolled in a Master's program.",
    # Hydrated text wraps mid-sentence; a newline must not hide the requirement.
    "Required qualifications currently enrolled\n&amp; in the process of obtaining a PhD in computer science.",
    # A far-off "preferred qualifications" heading must not release a basic qualification.
    "Basic Qualifications: Currently enrolled in an accredited PhD program. Preferred Qualifications: published research.",
    "What we need to see: pursuing a PhD degree in CS, CE, EE, or equivalent program.",
])
def test_explicit_graduate_only_wording_is_hard_ineligible(text):
    decision = classify_hard_eligibility({"title": "Intern", "description": text}, UNDERGRAD)
    assert decision["status"] == "INELIGIBLE", text
    assert decision["reason"] == "graduate_degree_required"


@pytest.mark.parametrize("title,text", [
    ("Software Engineer Intern", "Open to undergraduate and graduate students."),
    ("Software Engineer Intern", "Currently pursuing a Bachelor's, Master's, or PhD in Computer Science."),
    ("Software Engineer Intern", "Graduate students are also welcome to apply."),
    ("Software Engineer Intern", "Currently pursuing a BS/MS in Computer Science or related field."),
    ("Software Engineer Intern", "Master's degree preferred."),
    ("Software Engineer Intern (BS/MS/PhD)", "Pursuing a degree in Computer Science."),
    ("Software Engineer Intern", "Currently pursuing an undergraduate degree; new graduate candidates not considered."),
    # Real-data shapes that the pre-fix regex wrongly rejected or that a naive fix would.
    ("Software Engineer Intern", "Who you are: a student pursuing a bachelor's, master's, or PhD in computer science."),
    ("Data Intern", "Candidate Qualifications Rising senior or second-year graduate student, graduating December 2027."),
    ("Software Engineer Intern", "Current junior, senior or master's student pursuing a degree in Computer Science."),
    ("Software Engineer Intern", "Academic Partnerships\nUndergraduate Students\nGraduate Students\nProfessors and Universities"),
    ("Software Engineering Intern", "Must be a US Citizen, permanent resident or be an MS student with work authorization (F1 Visa on CPT accepted only for masters-level students)"),
])
def test_graduate_students_merely_welcome_is_not_a_hard_requirement(title, text):
    decision = classify_hard_eligibility({"title": title, "description": text}, UNDERGRAD)
    assert decision["status"] == "ELIGIBLE", text


GRAD_PROFILE = {"education": {"degree": "Bachelor of Science", "graduation": "2028 May"}, "work_preferences": {"sponsorship_now": False, "sponsorship_future": False}}


def test_gcm_grosvenor_graduation_window_is_hard_ineligible():
    # Real regression: GCM Grosvenor job f3620e3aec30f5b6 ranked ELIGIBLE (fit 91.3)
    # although it requires graduation Dec 2028 - Spring 2029 (candidate: May 2028).
    decision = classify_hard_eligibility({"title": "Software Engineer Intern", "description": "Requirements: Undergraduate students, grad date: Dec 2028 – Spring 2029."}, GRAD_PROFILE)
    assert decision["status"] == "INELIGIBLE"
    assert decision["reason"] == "graduation_window_mismatch"
    assert "Dec 2028" in decision["hard_requirements"][0]["evidence"]


@pytest.mark.parametrize("text", [
    "slated to graduate no earlier than December 2027.",
    "Graduating between December 2027 and June 2028.",
    "Expected graduation date: Spring 2028 - Summer 2028.",
    "Must be graduating in 2028 or later.",  # not an explicit window: never a gate
    "For undergrads: graduation between Dec 2028 and May 2029; for masters: graduation between Dec 2027 and Jun 2028.",
])
def test_graduation_windows_that_admit_candidate_or_are_vague_are_eligible(text):
    assert classify_hard_eligibility({"title": "Intern", "description": text}, GRAD_PROFILE)["status"] == "ELIGIBLE", text


@pytest.mark.parametrize("text", [
    "Graduating between December 2028 and June 2029.",
    "You must graduate no later than December 2027.",
])
def test_graduation_windows_that_exclude_candidate_are_ineligible(text):
    assert classify_hard_eligibility({"title": "Intern", "description": text}, GRAD_PROFILE)["reason"] == "graduation_window_mismatch", text


def test_active_clearance_required_is_ineligible_and_obtainable_needs_confirmation():
    held = classify_hard_eligibility({"description": "An active Secret clearance is required for this role."}, GRAD_PROFILE)
    assert (held["status"], held["reason"]) == ("INELIGIBLE", "security_clearance_required")
    obtain = classify_hard_eligibility({"description": "Candidates must have the ability to obtain a security clearance."}, GRAD_PROFILE)
    assert (obtain["status"], obtain["reason"]) == ("NEEDS_CONFIRMATION", "security_clearance_eligibility_needs_confirmation")


def test_mandatory_location_outside_profile_needs_confirmation():
    profile = {**GRAD_PROFILE, "location": "San Jose, CA"}
    assert classify_hard_eligibility({"description": "Candidates must reside in Texas."}, profile)["reason"] == "mandatory_location_needs_confirmation"
    assert classify_hard_eligibility({"description": "You must be located in the United States."}, profile)["status"] == "ELIGIBLE"
    assert classify_hard_eligibility({"description": "Must currently reside in California."}, profile)["status"] == "ELIGIBLE"


def test_student_visa_exclusion_needs_confirmation_but_plain_no_sponsorship_does_not():
    excluded = classify_hard_eligibility({"description": "We are unable to accept candidates on F-1 OPT or CPT."}, GRAD_PROFILE)
    assert (excluded["status"], excluded["reason"]) == ("NEEDS_CONFIRMATION", "student_visa_restriction_needs_confirmation")
    plain = classify_hard_eligibility({"description": "Visa sponsorship is not available for this position."}, GRAD_PROFILE)
    assert plain["status"] == "ELIGIBLE"
