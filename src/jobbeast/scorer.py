from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
try:
    from rapidfuzz import fuzz
except ImportError:  # Keep offline/demo mode usable with the standard library.
    from difflib import SequenceMatcher

    class _Fuzz:
        @staticmethod
        def token_set_ratio(left: str, right: str) -> float:
            a, b = set((left or "").lower().split()), set((right or "").lower().split())
            return 100.0 * SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()

    fuzz = _Fuzz()

from .profile import load_profile, load_projects, evidence_skill_set


def _text(row) -> str:
    return " ".join([
        row["title"] or "", row["company"] or "", row["description"] or "",
        " ".join(json.loads(row["locations_json"] or "[]")),
    ]).lower()


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return None


def _posted_basis(row) -> str | None:
    """The posting time, or first_seen when the source gave only a date but
    the listing came from a posted-within-N-minutes search."""
    keys = row.keys()
    if row["date_posted"]:
        return row["date_posted"]
    meta = json.loads(row["metadata_json"] or "{}") if "metadata_json" in keys else {}
    if meta.get("source_window_minutes") and meta.get("source_posted_precision") in ("DATE_ONLY", "UNKNOWN") and "first_seen" in keys:
        return row["first_seen"]
    return None


def score_job(row) -> dict:
    profile = load_profile()
    projects = load_projects(True)
    skills = evidence_skill_set()
    text = _text(row)

    # Required/evidenced terms: known evidence terms that the posting actually mentions.
    mentioned = sorted(s for s in skills if len(s) >= 2 and re.search(r"(?<!\w)" + re.escape(s) + r"(?!\w)", text, re.I))

    target_titles = profile.get("target_roles", [])
    title_score = max((fuzz.token_set_ratio(row["title"], t) for t in target_titles), default=0) / 100.0

    # Evidence coverage rewards jobs that mention many skills already backed by proof.
    # The denominator is capped so broad JDs do not punish the candidate simply for being verbose.
    skill_score = min(1.0, len(mentioned) / 8.0)

    age = _age_days(_posted_basis(row))
    if age is None:
        freshness = 0.45
    else:
        freshness = math.exp(-age / 8.0)

    # Internship/co-op wording is important for this search.
    internship_signal = 1.0 if any(x in text for x in ["intern", "internship", "co-op", "coop"]) else 0.25

    total = 100 * (0.40 * title_score + 0.35 * skill_score + 0.15 * freshness + 0.10 * internship_signal)

    # Project relevance by skill/tag overlap with the JD.
    project_matches = []
    for p in projects:
        terms = [str(x).lower() for x in (p.get("skills", []) + p.get("tags", []))]
        hits = sorted({term for term in terms if term and term in text})
        project_matches.append({"name": p.get("name"), "hits": hits, "score": len(hits)})
    project_matches.sort(key=lambda x: (-x["score"], x["name"] or ""))

    return {
        "score": round(total, 1),
        "title_match": round(title_score * 100, 1),
        "evidenced_keywords": mentioned,
        "freshness_days": None if age is None else round(age, 1),
        "top_projects": project_matches[:4],
    }
