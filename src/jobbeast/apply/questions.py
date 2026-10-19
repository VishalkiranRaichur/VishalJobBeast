"""Conservative application-question classification."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import re


CATEGORIES = {
    "IDENTITY", "CONTACT", "EDUCATION", "EXPERIENCE", "SKILL",
    "WORK_AUTHORIZATION", "SPONSORSHIP", "RELOCATION", "COMPENSATION",
    "DEMOGRAPHIC", "COMPANY_SPECIFIC", "ROLE_SPECIFIC", "BEHAVIORAL",
    "FREE_RESPONSE", "UNKNOWN",
}


@dataclass(frozen=True)
class Question:
    question: str
    category: str
    required: bool
    answer_source: str | None
    confidence: float

    def as_dict(self) -> dict:
        return asdict(self)


def classify_question(text: str, required: bool = False) -> Question:
    low = re.sub(r"\s+", " ", text.lower()).strip()
    rules = [
        ("WORK_AUTHORIZATION", ("authorized to work", "legally allowed", "work in the"), 0.99),
        ("SPONSORSHIP", ("sponsorship", "sponsor your", "visa"), 0.99),
        ("DEMOGRAPHIC", ("gender", "race", "ethnicity", "veteran", "disability", "sexual orientation"), 0.98),
        ("COMPENSATION", ("salary", "compensation", "pay expectation", "hourly rate"), 0.97),
        ("RELOCATION", ("relocate", "relocation", "willing to move", "commute"), 0.96),
        ("CONTACT", ("email", "phone", "telephone", "address", "city", "state", "zip"), 0.99),
        ("IDENTITY", ("full name", "first name", "last name", "preferred name"), 0.99),
        ("EDUCATION", ("school", "university", "degree", "major", "graduation", "gpa"), 0.96),
        ("EXPERIENCE", ("employer", "company", "work history", "employment history", "job title"), 0.95),
        ("SKILL", ("skill", "technology", "programming language", "experience with"), 0.82),
        ("BEHAVIORAL", ("tell us about a time", "challenge", "conflict", "feedback", "team"), 0.82),
        ("FREE_RESPONSE", ("why do you", "why this", "describe", "explain", "interest", "project"), 0.75),
    ]
    for category, needles, confidence in rules:
        if any(needle in low for needle in needles):
            return Question(text, category, required, None, confidence)
    return Question(text, "UNKNOWN", required, None, 0.0)
