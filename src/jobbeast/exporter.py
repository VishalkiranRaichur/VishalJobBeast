from __future__ import annotations

import csv
from pathlib import Path

from .profile import load_profile, load_projects
from .store import application_rows

ROOT = Path(__file__).resolve().parents[2]


def export_applications() -> Path:
    path = ROOT / "exports" / "applications.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = application_rows()
    columns = ["job_id", "company", "title", "url", "fit_score", "status", "resume_variant", "applied_at", "outcome", "notes", "updated_at"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for row in rows:
            w.writerow({c: row[c] for c in columns})
    return path


def export_mads_profile() -> Path:
    p = load_profile()
    projects = load_projects(True)
    path = ROOT / "exports" / "mads_candidate_profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    project_lines = []
    for proj in projects:
        project_lines.append(f"### {proj.get('name')}")
        for claim in proj.get("claims", []):
            project_lines.append(f"- {claim.get('text')}")
        project_lines.append("")
    text = f"""# Candidate Profile\n\n## Identity\n- **Name:** {p.get('name')}\n- **GitHub:** {p.get('links',{}).get('github')}\n- **LinkedIn:** {p.get('links',{}).get('linkedin')}\n\n## Education\n- **Institution:** {p.get('education',{}).get('institution')}\n- **Major:** {p.get('education',{}).get('major')}\n- **Degree:** {p.get('education',{}).get('degree')}\n- **Expected graduation:** {p.get('education',{}).get('graduation')}\n\n## Independent Projects\n\n{chr(10).join(project_lines)}\n## Technical Skills\n{', '.join(p.get('core_skills', []))}\n\n> Generated from Job Beast verified evidence. Fill private contact/legal fields manually in the private Mads workspace.\n"""
    path.write_text(text, encoding="utf-8")
    return path
