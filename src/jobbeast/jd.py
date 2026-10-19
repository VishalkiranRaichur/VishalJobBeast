"""Complete job-description retrieval metadata and requirement extraction.

The parser is deliberately conservative: it preserves employer wording and only
normalizes terms from an explicit alias table. It is not an instruction-following
parser; job-posting text is treated as untrusted data.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import re
from typing import Iterable


ALIASES: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "pandas": ("pandas",),
    "scikit-learn": ("scikit-learn", "sklearn", "scikit learn"),
    "sql": ("sql",),
    "statistics": ("statistics", "statistical",),
    "machine learning": ("machine learning", "ml models",),
    "classification": ("classification", "classifiers",),
    "model evaluation": ("evaluation metrics", "model evaluation", "false positive rates", "beyond accuracy"),
    "data analysis": ("data analysis", "analyze data", "explore relevant data"),
    "behavioral data": ("behavioral datasets", "behavioral data", "customer behavior", "gameplay data"),
    "monitoring": ("monitoring", "dashboards", "track your signal"),
    "sql data shaping": ("pull and shape your own data", "write sql", "large datasets"),
    "adversarial problems": ("adversarial problems", "game a system", "abusive activity", "fraud and abuse"),
    "nlp": ("nlp", "natural language processing", "text classification"),
    "anomaly detection": ("anomaly detection",),
    "graph/network analysis": ("graph or network analysis", "graph analysis", "network analysis"),
    "sequence/time-series modeling": ("sequence and time-series modeling", "time-series modeling"),
    "trust and safety": ("trust and safety", "online trust", "ecosystem security"),
    "games/live services": ("games", "live services"),
    "technical communication": ("communicating technical findings", "technical findings", "present your results"),
    "feedback": ("constructive feedback",),
    "messy/sparsely labeled data": ("messy", "sparsely labeled data"),
    "work authorization": ("legally authorized to work", "work authorization"),
    "react": ("react",),
    "fastapi": ("fastapi",),
    "docker": ("docker",),
    "graphql": ("graphql",),
    "data visualization": ("visualization", "visualizations", "dashboard"),
    "education requirement": ("bachelor's degree", "bachelor's", "undergraduate degree", "currently enrolled", "pursuing a degree"),
    "graduation timing": ("graduation", "graduate by", "expected graduation"),
}

REQUIREMENT_CATEGORIES = {
    "TECHNICAL_REQUIREMENT",
    "DOMAIN_SIGNAL",
    "RESPONSIBILITY",
    "EDUCATION_REQUIREMENT",
    "ELIGIBILITY_LOGISTICS",
    "NICE_TO_HAVE",
}


@dataclass(frozen=True)
class Requirement:
    id: str
    priority: str
    original: str
    normalized: str
    category: str
    source_phrase: str

    def as_dict(self) -> dict:
        return asdict(self)


def _lines(description: str) -> list[str]:
    raw = re.split(r"\n+|(?<=[.!?])\s+", description or "")
    return [re.sub(r"\s+", " ", line).strip(" -*•\t") for line in raw if line.strip()]


def _priority(line: str, normalized: str) -> str:
    low = line.lower()
    if normalized in {"pandas", "scikit-learn"} and "preferred" in low:
        return "PREFERRED"
    if any(x in low for x in ("must", "required", "what we're looking for", "experience with", "ability to", "grounding in")):
        return "MUST_HAVE"
    if any(x in low for x in ("nice to have", "preferred", "prefer")):
        return "PREFERRED"
    if normalized in {"work authorization", "feedback", "technical communication", "messy/sparsely labeled data"}:
        return "IMPORTANT"
    if normalized in {"trust and safety", "games/live services"}:
        return "CONTEXTUAL"
    return "IMPORTANT"


def _category(normalized: str, original: str) -> str:
    """Classify an extracted item without treating every JD sentence as a skill gap."""
    low = original.lower()
    if normalized in {"work authorization", "graduation timing"} or any(term in low for term in (
        "legally authorized", "work authorization", "relocation", "relocate", "sponsorship",
        "location", "apply", "application", "start date", "employment eligibility",
    )):
        return "ELIGIBILITY_LOGISTICS"
    if normalized == "education requirement" or any(term in low for term in (
        "bachelor", "degree", "currently enrolled", "pursuing a degree", "major in", "coursework",
    )):
        return "EDUCATION_REQUIREMENT"
    if normalized in {"pandas", "scikit-learn"} and "preferred" in low:
        return "NICE_TO_HAVE"
    if any(term in low for term in ("nice to have", "nice-to-have", "bonus", "plus")):
        return "NICE_TO_HAVE"
    if normalized in {"adversarial problems", "behavioral data", "trust and safety", "games/live services"}:
        return "DOMAIN_SIGNAL"
    if normalized in {"python", "pandas", "scikit-learn", "sql", "statistics", "machine learning", "classification", "model evaluation", "data analysis", "nlp", "anomaly detection", "graph/network analysis", "sequence/time-series modeling", "sql data shaping", "monitoring", "data visualization", "react", "fastapi", "docker", "graphql"}:
        return "TECHNICAL_REQUIREMENT"
    if normalized in {"technical communication", "feedback", "messy/sparsely labeled data"}:
        return "RESPONSIBILITY"
    return "RESPONSIBILITY"


def parse_job_description(*, title: str, company: str, location: str, url: str, description: str, retrieved_at: str | None = None) -> dict:
    retrieved_at = retrieved_at or datetime.now(timezone.utc).isoformat()
    lines = _lines(description)
    requirements: list[Requirement] = []
    seen: set[str] = set()
    req_num = 1
    lower = description.lower()
    for normalized, phrases in ALIASES.items():
        matched = next((phrase for phrase in phrases if phrase.lower() in lower), None)
        if not matched or normalized in seen:
            continue
        if normalized == "react" and re.search(r"react\s+and\s+respond", lower):
            continue
        original = next((line for line in lines if matched.lower() in line.lower()), matched)
        requirements.append(Requirement(
            id=f"REQ-{req_num:03d}",
            priority=_priority(original, normalized),
            original=original,
            normalized=normalized,
            category=_category(normalized, original),
            source_phrase=matched,
        ))
        seen.add(normalized)
        req_num += 1

    # Preserve the employer's responsibility language even when it has no known
    # skill alias. This helps reviewers see what was parsed and what is unsupported.
    for line in lines:
        low = line.lower()
        if any(token in low for token in ("you'll", "you will", "in this role", "responsibilit", "this internship")):
            key = re.sub(r"[^a-z0-9]+", " ", low).strip()
            if key not in seen:
                requirements.append(Requirement(
                    id=f"REQ-{req_num:03d}", priority="CONTEXTUAL", original=line,
                    normalized="responsibility/context", category=_category("responsibility/context", line), source_phrase=line,
                ))
                seen.add(key)
                req_num += 1
    return {
        "job": {"title": title, "company": company, "location": location, "url": url, "retrieved_at": retrieved_at},
        "description": description,
        "requirements": [req.as_dict() for req in requirements],
        "normalization_aliases": {k: list(v) for k, v in ALIASES.items()},
    }
