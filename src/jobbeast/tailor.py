from __future__ import annotations

from pathlib import Path
import json
import re

from .profile import load_profile, load_projects
from .scorer import score_job


ROOT = Path(__file__).resolve().parents[2]

# (job-text signals, project type/tag markers): a verified project whose type or
# tags carry a marker gets a boost when the job text mentions a signal.
DOMAIN_BOOSTS: list[tuple[list[str], list[str]]] = [
    (["database", "sql", "backend", "storage"], ["database", "storage"]),
    (["distributed", "infrastructure", "platform", "ci/cd", "scheduler"], ["distributed", "platform", "scheduler", "ci"]),
    (["rag", "nlp", "document", "retrieval"], ["rag", "nlp", "retrieval", "document"]),
    (["reliability", "incident", "stripe", "fintech", "agent", "workflow"], ["reliability", "incident", "agent", "workflow"]),
]


def _job_text(row) -> str:
    return (row["title"] + " " + row["company"] + " " + (row["description"] or "")).lower()


def choose_projects(row, limit: int = 4) -> list[dict]:
    text = _job_text(row)
    ranked = []
    for project in load_projects(True):
        terms = [str(x).lower() for x in (project.get("skills", []) + project.get("tags", []))]
        hits = sorted({x for x in terms if x and x in text})
        # Light title/domain weighting even if a feed lacks full description.
        name = (project.get("name") or "").lower()
        score = len(hits)
        markers = " ".join([name, str(project.get("type", "")).lower(), *[str(t).lower() for t in project.get("tags", [])]])
        for job_signals, project_signals in DOMAIN_BOOSTS:
            if any(x in text for x in job_signals) and any(m in markers for m in project_signals):
                score += 3
        ranked.append((score, hits, project))
    ranked.sort(key=lambda t: (-t[0], t[2].get("name", "")))
    return [{"score": s, "hits": h, **p} for s, h, p in ranked[:limit]]


def make_packet(row) -> Path:
    profile = load_profile()
    report = score_job(row)
    projects = choose_projects(row)
    out_dir = ROOT / "output" / row["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "application_packet.md"

    locations = ", ".join(json.loads(row["locations_json"] or "[]")) or "Not provided"
    project_sections = []
    for p in projects:
        claims = "\n".join(f"  - {c.get('text')}  \n    Evidence: {c.get('evidence')}" for c in p.get("claims", []))
        project_sections.append(
            f"### {p.get('name')}\nMatch signals: {', '.join(p.get('hits', [])) or 'title/domain heuristic'}\n"
            f"Verified skills: {', '.join(p.get('skills', []))}\n{claims}"
        )

    desc = row["description"] or "(The source feed did not include a full description. Open the official posting URL before final drafting.)"
    text = f"""# Application Packet\n\n## Job\n- Company: {row['company']}\n- Role: {row['title']}\n- Location: {locations}\n- Source: {row['source']}\n- Posted: {row['date_posted'] or 'unknown'}\n- URL: {row['url']}\n- Current fit score: {report['score']} / 100\n\n## Candidate\n- Name: {profile.get('name')}\n- Headline: {profile.get('headline')}\n- School: {profile.get('education',{}).get('institution')}\n- Major: {profile.get('education',{}).get('major')}\n- GitHub: {profile.get('links',{}).get('github')}\n\n## Evidenced keyword hits\n{', '.join(report['evidenced_keywords']) or 'No full-description evidence yet; fetch/open the posting before final drafting.'}\n\n## Recommended verified projects\n\n{chr(10).join(project_sections)}\n\n## Grounding rules for the drafting agent\n- Use only profile facts and verified project claims above or in the vault.\n- Rephrase for the role, but never invent a skill, metric, project feature, date, employer, or experience.\n- Projects marked verified: false in the vault must not be used until a source is attached.\n- If the job asks for a missing technology, do not add it to Skills. Flag it as a gap or propose a proof project outside the resume.\n- Prefer a one-page ATS-friendly layout.\n- Use the employer's wording when it truthfully names the same capability.\n\n## Job description\n\n{desc}\n"""
    out.write_text(text, encoding="utf-8")
    return out
