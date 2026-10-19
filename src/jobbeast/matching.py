"""Requirement-to-evidence matching, ranking, and auditable coverage reports."""
from __future__ import annotations

import re
from collections import defaultdict

from .evidence import load_evidence, evidence_text
from .jd import ALIASES, parse_job_description


MATCH_TYPES = {"SUPPORTED_EXACT", "SUPPORTED_UMBRELLA", "SUPPORTED_ADJACENT", "UNSUPPORTED"}

UMBRELLA: dict[str, tuple[str, ...]] = {
    "machine learning": ("scikit-learn", "machine learning", "tf-idf"),
    "data analysis": ("analytics", "dashboard", "data integration", "visualization"),
    "data visualization": ("plotly", "visualization", "dashboard"),
    "nlp": ("text processing", "information retrieval", "tf-idf", "retrieval-augmented generation"),
    "monitoring": ("monitoring", "dashboard", "grafanalib"),
    "behavioral data": ("customer behavior", "behavioral data", "customer impact"),
    "sql data shaping": ("sql", "data integration"),
    "technical communication": ("workshop", "inspectable", "documentation"),
}

ADJACENT: dict[str, tuple[str, ...]] = {
    "pandas": ("numpy", "analytics", "data integration"),
    "classification": ("machine learning", "tf-idf"),
    "model evaluation": ("testing", "verification", "inspectable"),
    "messy/sparsely labeled data": ("text processing", "user-generated data"),
    "adversarial problems": ("policy", "idempotency", "verification"),
    "trust and safety": ("policy", "customer impact", "verification"),
    "technical communication": ("workshop", "dashboard"),
}


def _terms(normalized: str) -> tuple[str, ...]:
    return tuple(x.lower() for x in ALIASES.get(normalized, (normalized,)))


def _wordish(term: str, text: str) -> bool:
    if " " in term or "/" in term or "-" in term:
        return term.lower() in text.lower()
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text.lower()) is not None


def match_requirement(requirement: dict, evidence: list[dict] | None = None) -> dict:
    evidence = evidence or load_evidence()
    normalized = requirement["normalized"]
    exact_ids = [item["id"] for item in evidence if any(_wordish(term, evidence_text(item)) for term in _terms(normalized))]
    if exact_ids:
        return {**requirement, "match_type": "SUPPORTED_EXACT", "evidence_ids": exact_ids}

    umbrella_terms = UMBRELLA.get(normalized, ())
    umbrella_ids = [item["id"] for item in evidence if any(_wordish(term, evidence_text(item)) for term in umbrella_terms)]
    if umbrella_ids:
        return {**requirement, "match_type": "SUPPORTED_UMBRELLA", "evidence_ids": umbrella_ids}

    adjacent_terms = ADJACENT.get(normalized, ())
    adjacent_ids = [item["id"] for item in evidence if any(_wordish(term, evidence_text(item)) for term in adjacent_terms)]
    if adjacent_ids:
        return {**requirement, "match_type": "SUPPORTED_ADJACENT", "evidence_ids": adjacent_ids}
    return {**requirement, "match_type": "UNSUPPORTED", "evidence_ids": []}


def build_requirement_matrix(job: dict, evidence: list[dict] | None = None) -> dict:
    # Job rows may include persistence metadata (id, score, ATS fields). The JD
    # parser accepts only the public posting fields, so keep this boundary
    # explicit instead of leaking SQLite columns into the parser.
    parsed = parse_job_description(
        title=str(job.get("title", "")),
        company=str(job.get("company", "")),
        location=str(job.get("location", "")),
        url=str(job.get("url", "")),
        description=str(job.get("description", "")),
        retrieved_at=job.get("retrieved_at") or job.get("jd_fetched_at"),
    )
    evidence = evidence or load_evidence()
    matches = [match_requirement(req, evidence) for req in parsed["requirements"]]
    weights = {"MUST_HAVE": 5.0, "IMPORTANT": 3.0, "PREFERRED": 2.0, "CONTEXTUAL": 1.0, "LOW_PRIORITY": 0.5}
    # Exact evidence is strongest, umbrella evidence is useful but broader, and
    # adjacent evidence is a transfer signal rather than an exact qualification.
    match_factors = {"SUPPORTED_EXACT": 1.0, "SUPPORTED_UMBRELLA": 0.75, "SUPPORTED_ADJACENT": 0.35, "UNSUPPORTED": 0.0}

    def category_report(category: str) -> dict:
        items = [item for item in matches if item["category"] == category]
        total = sum(weights.get(item["priority"], 1.0) for item in items)
        supported = sum(weights.get(item["priority"], 1.0) * match_factors[item["match_type"]] for item in items)
        exact = sum(weights.get(item["priority"], 1.0) for item in items if item["match_type"] == "SUPPORTED_EXACT")
        counts = {kind: sum(item["match_type"] == kind for item in items) for kind in MATCH_TYPES}
        return {
            "weighted": round(supported / total, 4) if total else 0.0,
            "exact": round(exact / total, 4) if total else 0.0,
            "counts": counts,
            "total_items": len(items),
            "weighted_items": round(total, 4),
        }

    category_reports = {category: category_report(category) for category in (
        "TECHNICAL_REQUIREMENT", "DOMAIN_SIGNAL", "RESPONSIBILITY",
        "EDUCATION_REQUIREMENT", "ELIGIBILITY_LOGISTICS", "NICE_TO_HAVE",
    )}
    technical = category_reports["TECHNICAL_REQUIREMENT"]["weighted"]
    domain = category_reports["DOMAIN_SIGNAL"]["weighted"]
    nice = category_reports["NICE_TO_HAVE"]["weighted"]
    responsibility = category_reports["RESPONSIBILITY"]["weighted"]
    # Eligibility and education are reported independently. They do not dilute
    # the technical/domain optimization score or create ATS keyword gaps.
    decision_score = 0.55 * technical + 0.30 * domain + 0.10 * nice + 0.05 * responsibility
    all_unsupported = sum(x["match_type"] == "UNSUPPORTED" for x in matches)
    technical_items = category_reports["TECHNICAL_REQUIREMENT"]["total_items"]
    return {
        **parsed,
        "matches": matches,
        "coverage": {
            "main_resume_optimization_score": round(decision_score, 4),
            "technical_coverage": category_reports["TECHNICAL_REQUIREMENT"],
            "domain_alignment": category_reports["DOMAIN_SIGNAL"],
            "responsibility_coverage": category_reports["RESPONSIBILITY"],
            "education_eligibility": category_reports["EDUCATION_REQUIREMENT"],
            "logistics_eligibility": category_reports["ELIGIBILITY_LOGISTICS"],
            "nice_to_have_coverage": category_reports["NICE_TO_HAVE"],
            "unsupported_requirements": all_unsupported,
            "unsupported_technical_requirements": sum(x["match_type"] == "UNSUPPORTED" and x["category"] == "TECHNICAL_REQUIREMENT" for x in matches),
            "unsupported_excluded": sum(x["match_type"] == "UNSUPPORTED" and x["category"] == "TECHNICAL_REQUIREMENT" for x in matches),
            "technical_items_excluded_from_logistics": len(matches) - technical_items,
        },
    }


def rank_evidence_groups(matrix: dict, group_kind: str) -> list[dict]:
    """Rank verified project/experience owners by requirement overlap."""
    evidence = load_evidence()
    relevant = [x for x in evidence if x.get("kind") == group_kind]
    weights = {"MUST_HAVE": 5.0, "IMPORTANT": 3.0, "PREFERRED": 2.0, "CONTEXTUAL": 1.0, "LOW_PRIORITY": 0.5}
    groups: dict[str, dict] = {}
    for item in relevant:
        owner = item.get("owner", "")
        groups.setdefault(owner, {"name": owner, "evidence_ids": [], "score": 0.0, "matched_requirements": []})
        groups[owner]["evidence_ids"].append(item["id"])
    for req in matrix["matches"]:
        if not req["evidence_ids"]: continue
        for group in groups.values():
            if set(group["evidence_ids"]) & set(req["evidence_ids"]):
                factor = 1.0 if req["match_type"] == "SUPPORTED_EXACT" else 0.75 if req["match_type"] == "SUPPORTED_UMBRELLA" else 0.35
                group["score"] += weights.get(req["priority"], 1.0) * factor
                group["matched_requirements"].append(req["normalized"])
    return sorted(groups.values(), key=lambda x: (-x["score"], x["name"]))


def requirement_locations(matrix: dict, selected_evidence_ids: set[str]) -> list[dict]:
    locations = []
    for req in matrix["matches"]:
        ids = sorted(set(req["evidence_ids"]) & selected_evidence_ids)
        if req["category"] == "ELIGIBILITY_LOGISTICS":
            action = "manual verification; exclude from ATS keyword scoring"
        elif req["match_type"] == "UNSUPPORTED":
            action = "exclude from resume; report as gap"
        elif req["match_type"] == "SUPPORTED_ADJACENT":
            action = "use adjacent evidence only; do not claim the exact requirement"
        elif ids:
            action = "represent with selected evidence"
        else:
            action = "supported in vault; add only if relevant to the tailored resume"
        locations.append({**req, "resume_location": "demonstrated" if ids else "not represented", "resume_evidence_ids": ids, "action": action})
    return locations
