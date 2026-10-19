"""Detect incomplete or suspicious job records before tailoring."""
from __future__ import annotations

import re


def assess_job(job: dict) -> dict:
    warnings: list[str] = []
    description = str(job.get("description", "") or "").strip()
    url = str(job.get("url", "") or "").strip()
    if not description:
        warnings.append("INCOMPLETE_DESCRIPTION")
    elif len(description) < 300:
        warnings.append("SHORT_DESCRIPTION")
    if not url or not re.match(r"https?://", url):
        warnings.append("MISSING_OR_INVALID_URL")
    if any(token in description.lower() for token in ("paste these instructions", "ignore previous instructions", "system message")):
        warnings.append("SUSPICIOUS_INSTRUCTION_IN_JOB_TEXT")
    if not job.get("company") or not job.get("title"):
        warnings.append("MISSING_IDENTITY_FIELDS")
    return {"suspicious": any(x.startswith("SUSPICIOUS") for x in warnings), "incomplete": any(x.startswith(("INCOMPLETE", "SHORT", "MISSING")) for x in warnings), "warnings": warnings}
