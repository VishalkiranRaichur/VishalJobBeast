"""Conservative hard eligibility checks applied before application preparation."""
from __future__ import annotations

import re
from typing import Any

_GRAD = r"(?:master'?s|masters|ph\.?\s?d\.?|doctoral|doctorate)"
# Wording that names graduate (Master's/PhD) enrollment as the requirement.
# `\bgraduate` never matches inside "undergraduate".
_GRADUATE_MANDATORY = re.compile(
    rf"\b{_GRAD}[- ]level\b|"  # "(Master's Level)"
    rf"(?<!new )(?<!recent )(?:\b{_GRAD}|\bm\.?s\.?|\bgraduate)\s+(?:students?|candidates?)\b|"  # "PhD student", "graduate students"
    rf"(?:enrolled|pursuing|progressing towards|working towards|in pursuit of)\b.{{0,100}}?(?:\bgraduate (?:program|degree|studies|school)|\b{_GRAD})|"
    rf"\b{_GRAD}.{{0,60}}(?:degree|program).{{0,40}}(?:required|must)|"
    rf"(?:required|must).{{0,60}}(?:\b{_GRAD}|\bgraduate (?:program|degree))",
    re.I | re.S,
)
# Anywhere in the same sentence: the requirement also admits undergraduates
# ("BS/MS", "undergraduate and graduate students", "junior, senior or
# master's student"), only welcomes graduate students alongside others, or
# is a visa condition rather than a degree gate.
_UNDERGRAD_ALLOWED = re.compile(
    r"undergrad|bachelor|baccalaureate|\bb\.?sc?\.?(?=[\s/,);]|$)|\bb\.?a\.?(?=[\s/,);]|$)|"
    r"\b(?:sophomores?|juniors?|seniors?)\b(?=\s*(?:,|/|or\b|and\b|that\b|who\b|students?\b|year\b|standing\b))|rising senior|"
    r"all (?:degree )?levels|any degree|also (?:welcome|encouraged|eligible|consider)|welcome to apply|encouraged to apply|"
    # A visa condition ("F1 on CPT accepted only for masters-level students")
    # restricts the visa route, not the degree level.
    r"\bf-?1\b|\bcpt\b|\bvisa\b",
    re.I,
)
# Directly attached to the graduate wording: the degree is a preference,
# not a gate ("Master's degree preferred"). "Preferred qualifications" is a
# section heading, not a qualifier of the match.
_GRADUATE_PREFERRED = re.compile(r"\b(?:preferred|a plus|bonus|nice[- ]to[- ]have)\b(?!\s*qualifications)", re.I)
# Sentence ends: a period followed by a capital (so "Ph.D." and "U.S." do
# not split) or a semicolon. Newlines are not boundaries -- hydrated text
# often wraps mid-sentence.
_SENTENCE_END = re.compile(r"(?<=[a-z0-9)])[.!?]\s+(?=[A-Z])|;")


def _normalize(text: str) -> str:
    return (text.replace("’", "'").replace("â€™", "'").replace("â\x80\x99", "'")
            .replace("&nbsp;", " ").replace("&amp;", "&"))


def _sentence_bounds(text: str, start: int, end: int, reach: int = 200) -> tuple[int, int]:
    lo, hi = max(0, start - reach), min(len(text), end + reach)
    ends_before = list(_SENTENCE_END.finditer(text, lo, start))
    after = _SENTENCE_END.search(text, end, hi)
    return (ends_before[-1].end() if ends_before else lo), (after.start() if after else hi)


def _mandatory_graduate_clause(text: str) -> str:
    """Return the sentence that explicitly requires graduate enrollment, or ""."""
    text = _normalize(text)
    for match in _GRADUATE_MANDATORY.finditer(text):
        start, end = _sentence_bounds(text, match.start(), match.end())
        if _UNDERGRAD_ALLOWED.search(text, start, end):
            continue
        line_start = max(start, text.rfind("\n", 0, match.start()) + 1, match.start() - 60)
        if _GRADUATE_PREFERRED.search(text, line_start, min(end, match.end() + 40)):
            continue
        return " ".join(text[start:end].split())[:200]
    return ""


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# Seasons are ambiguous, so each maps to the widest plausible month range;
# a window is only ever made more lenient by that ambiguity.
_SEASONS = {"spring": (1, 6), "summer": (5, 8), "fall": (8, 12), "autumn": (8, 12), "winter": (-1, 3)}
_WHEN = r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*|(?:spring|summer|fall|autumn|winter)\s+)?20\d\d"
_WINDOW = re.compile(
    rf"grad(?:uation|uating)?(?:\s+dates?)?\s*(?::|-|of|between|from)?\s*({_WHEN})\s*(?:-|–|—|to|through|and)\s*({_WHEN})|"
    rf"graduat\w*[^.;\n]{{0,40}}?no earlier than\s+({_WHEN})|"
    rf"graduat\w*[^.;\n]{{0,40}}?no later than\s+({_WHEN})",
    re.I,
)


def _when_range(text: str) -> tuple[int, int]:
    """(earliest, latest) month index (year*12+month) a date expression can mean."""
    text = text.lower().strip()
    year = int(re.search(r"20\d\d", text).group(0))
    for season, (lo, hi) in _SEASONS.items():
        if text.startswith(season):
            return year * 12 + lo, year * 12 + hi
    month = re.match(r"([a-z]{3})", text)
    if month and month.group(1) in _MONTHS:
        value = year * 12 + _MONTHS[month.group(1)]
        return value, value
    return year * 12 + 1, year * 12 + 12


def _profile_graduation(profile: dict[str, Any]) -> int | None:
    raw = str(profile.get("education", {}).get("graduation", "") or "")
    year = re.search(r"20\d\d", raw)
    month = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", raw.lower())
    if not year or not month:
        return None
    return int(year.group(0)) * 12 + _MONTHS[month.group(1)]


def graduation_window_conflict(text: str, profile: dict[str, Any]) -> str:
    """Quote of an explicit graduation window the candidate falls outside of, or "".

    Only explicit windows count ("grad date: Dec 2028 – Spring 2029",
    "graduating between ...", "no earlier/later than ..."). When several
    windows appear, the candidate passes if any one of them admits them.
    """
    grad = _profile_graduation(profile)
    if grad is None:
        return ""
    conflicts = []
    for match in _WINDOW.finditer(_normalize(text)):
        lo_text, hi_text, earliest, latest = match.groups()
        lo = _when_range(lo_text or earliest)[0] if (lo_text or earliest) else None
        hi = _when_range(hi_text or latest)[1] if (hi_text or latest) else None
        if (lo is None or grad >= lo) and (hi is None or grad <= hi):
            return ""
        conflicts.append(" ".join(match.group(0).split()))
    return conflicts[0] if conflicts else ""


_CLEARANCE_KIND = r"(?:dod\s+)?(?:secret|top[- ]secret|ts/sci|ts|security|government)(?:\s*/\s*sci)?\s+(?:security\s+)?clearance"
_CLEARANCE_HELD = re.compile(
    rf"(?:active|current)\s+{_CLEARANCE_KIND}[^.;\n]{{0,40}}(?:required|is required|must)|"
    rf"(?:must|required to)\s+(?:currently\s+)?(?:have|hold|possess)\s+(?:an?\s+)?(?:active\s+)?{_CLEARANCE_KIND}", re.I)
_CLEARANCE_OBTAIN = re.compile(rf"(?:ability|able|eligible|eligibility)\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:an?\s+)?{_CLEARANCE_KIND}", re.I)
_LOCATION_REQUIRED = re.compile(
    r"must\s+(?:currently\s+)?(?:reside|live|be located|be based|be a resident)\s+(?:in|within|of)\s+(?:the\s+)?([A-Za-z][A-Za-z .,'-]{2,40})", re.I)
_STUDENT_VISA_EXCLUDED = re.compile(
    r"(?:not|unable to|cannot|will not)\s+(?:be\s+)?(?:able\s+to\s+)?(?:accept|consider|support|hire)[^.;\n]{0,40}\b(?:f-?1|opt|cpt|student visas?)\b|"
    r"\b(?:f-?1|opt|cpt)\b[^.;\n]{0,40}(?:are|is)\s+not\s+(?:eligible|accepted|considered)", re.I)


def _matches_profile_location(place: str, profile: dict[str, Any]) -> bool:
    place = place.lower()
    location = profile.get("location") or {}
    known = {"united states", "u.s", "usa", "us", "the us", "america", "california", "ca", "bay area", "san francisco bay area"}
    if isinstance(location, dict):
        known |= {str(v).lower() for v in location.values() if v}
    elif location:
        known |= {part.strip().lower() for part in str(location).split(",") if part.strip()}
    return any(re.search(rf"\b{re.escape(k)}\b", place) for k in known if k)


def classify_hard_eligibility(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    title = str(job.get("title", "") or "")
    # The title alone frequently carries a hard requirement a short/incomplete
    # description won't ("PhD Machine Learning Engineer Intern") -- scan both.
    text = f"{title}\n{job.get('description', '') or ''}"
    low = text.lower().replace("’", "'").replace("&nbsp;", " ")
    title_low = title.lower().replace("’", "'")
    hard: list[dict[str, str]] = []

    us_person_required = bool(re.search(
        r"(?:u\.s\.?\s*(?:citizen|person)|us person|citizenship|lawful permanent resident|green card|refugee/asylee|refugee or asylee).{0,100}(?:required|must|eligib)|(?:required|must).{0,100}(?:u\.s\.?\s*(?:citizen|person)|citizenship|lawful permanent resident|green card|refugee/asylee|refugee or asylee)",
        low,
    ))
    export_control = bool(re.search(r"itar|ear\s+(?:eligible|eligibility)|export control|export regulations|u\.s\.? person", low))
    if us_person_required or export_control and re.search(r"citizen|permanent resident|green card|refugee|asylee|required|must", low):
        hard.append({"code": "export_control_us_person_requirement", "status": "INELIGIBLE", "evidence": "Posting requires U.S. citizenship, permanent residency, protected individual status, or equivalent export-control eligibility."})

    no_sponsorship = bool(re.search(r"(?:no|not provide|unable to provide|will not provide).{0,50}sponsorship|sponsorship.{0,50}(?:not available|unavailable|required)", low))
    sponsorship_required = bool(profile.get("work_preferences", {}).get("sponsorship_now") or profile.get("work_preferences", {}).get("sponsorship_future"))
    if no_sponsorship and sponsorship_required:
        hard.append({"code": "sponsorship_unavailable", "status": "INELIGIBLE", "evidence": "Posting does not provide sponsorship and the confirmed profile requires it."})

    # Only explicit, mandatory graduate enrollment is a hard gate.  Each clause
    # is judged on its own, and a clause that also admits undergraduates or
    # frames the graduate degree as optional ("graduate students are also
    # welcome", "BS/MS", "Master's preferred") never counts.
    graduate_clause = _mandatory_graduate_clause(text)
    # A title that itself names a graduate degree ("PhD Research Intern") is a
    # strong standalone signal, independent of how little description text
    # made it through hydration.
    title_graduate_marker = bool(re.search(r"\b(?:ph\.?d|doctoral|master'?s)\b", title_low)) and not _UNDERGRAD_ALLOWED.search(title_low)
    profile_degree = str(profile.get("education", {}).get("degree", "")).lower()
    if (graduate_clause or title_graduate_marker) and not re.search(r"master|ph\.?d|doctor", profile_degree):
        evidence = (
            f"Posting requires graduate-level (Master's/PhD) enrollment: \"{graduate_clause}\"; verified profile is undergraduate."
            if graduate_clause
            else f"Job title explicitly names a graduate-level requirement ('{title}'); verified profile is undergraduate."
        )
        hard.append({"code": "graduate_degree_required", "status": "INELIGIBLE", "evidence": evidence})

    high_school_only = bool(re.search(r"(?:currently enrolled in high school|high school students|graduating from high school).{0,80}(?:minimum|required|internship)", low))
    if high_school_only and not re.search(r"high school", profile_degree):
        hard.append({"code": "high_school_requirement", "status": "INELIGIBLE", "evidence": "Posting is restricted to current or graduating high-school students; verified profile is a university undergraduate."})

    window_conflict = graduation_window_conflict(text, profile)
    if window_conflict:
        hard.append({"code": "graduation_window_mismatch", "status": "INELIGIBLE", "evidence": f"Posting requires graduation in a window that excludes the verified graduation ({profile.get('education', {}).get('graduation')}): \"{window_conflict}\"."})

    held = _CLEARANCE_HELD.search(text)
    if held and not profile.get("security_clearance"):
        hard.append({"code": "security_clearance_required", "status": "INELIGIBLE", "evidence": f"Posting requires an existing security clearance: \"{held.group(0)}\"; the verified profile holds none."})
    elif _CLEARANCE_OBTAIN.search(text):
        hard.append({"code": "security_clearance_eligibility_needs_confirmation", "status": "NEEDS_CONFIRMATION", "evidence": f"Posting requires the ability to obtain a clearance (\"{_CLEARANCE_OBTAIN.search(text).group(0)}\"); clearance eligibility is a legal question that is never inferred."})

    for located in _LOCATION_REQUIRED.finditer(text):
        if not _matches_profile_location(located.group(1), profile):
            hard.append({"code": "mandatory_location_needs_confirmation", "status": "NEEDS_CONFIRMATION", "evidence": f"Posting requires residence/location: \"{' '.join(located.group(0).split())}\"."})
            break

    visa = _STUDENT_VISA_EXCLUDED.search(text)
    if visa:
        hard.append({"code": "student_visa_restriction_needs_confirmation", "status": "NEEDS_CONFIRMATION", "evidence": f"Posting restricts student-visa (F-1/OPT/CPT) candidates: \"{' '.join(visa.group(0).split())}\"; immigration answers are never inferred."})

    non_us_work_auth = re.search(r"(?:legally authorized|authorized to work|right to work).{0,60}(?:united kingdom|\buk\b|canada|australia|europe|ireland)", low)
    if non_us_work_auth:
        hard.append({"code": "non_us_work_authorization_needs_confirmation", "status": "NEEDS_CONFIRMATION", "evidence": "Posting asks for work authorization in a country outside the confirmed U.S. work-authorization context."})

    status = "INELIGIBLE" if any(item["status"] == "INELIGIBLE" for item in hard) else ("NEEDS_CONFIRMATION" if hard else "ELIGIBLE")
    return {"status": status, "hard_requirements": hard, "reason": hard[0]["code"] if hard else ""}
