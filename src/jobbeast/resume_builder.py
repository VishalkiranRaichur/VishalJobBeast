"""Dynamic, evidence-linked LaTeX resume construction.

The builder accepts a job, a selected bullet plan, and a dynamic skill list. The
plan is kept separate from the candidate vault so a tailored version never
mutates the master history.
"""
from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from copy import deepcopy

from .evidence import evidence_by_id
from .matching import build_requirement_matrix, rank_evidence_groups, requirement_locations
from .profile import forbidden_resume_terms, load_profile, load_projects


ROOT = Path(__file__).resolve().parents[2]


def latex_escape(value: str) -> str:
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "·": r"\textperiodcentered{}"}
    return "".join(replacements.get(ch, ch) for ch in str(value))


def _item(ident: str, text: str) -> dict:
    return {"evidence_ids": [ident], "text": text}


def auto_plan(job: dict, matrix: dict | None = None, project_limit: int = 3) -> dict:
    """Build a conservative generic plan from evidence IDs.

    This baseline intentionally prefers the documented default experiences and
    uses complete verified evidence claims as bullets. A richer UI can let a
    reviewer edit the resulting plan before compilation.
    """
    matrix = matrix or build_requirement_matrix(job)
    evidence = evidence_by_id()
    exp_groups = rank_evidence_groups(matrix, "experience")
    by_owner = {group["name"]: group for group in exp_groups}
    # Prefer experiences in the order the candidate lists them in profile.yaml.
    preferred = [str(x.get("organization")) for x in load_profile().get("experience", []) if x.get("verified", True)]
    exp_owners = [name for name in preferred if name in by_owner]
    exp_owners = exp_owners[:2] if len(exp_owners) >= 2 else exp_owners
    experiences = []
    for owner in exp_owners:
        ids = by_owner[owner]["evidence_ids"]
        items = [evidence[i] for i in ids][:3]
        profile_exp = next((x for x in load_profile().get("experience", []) if x.get("organization") == owner), {})
        experiences.append({
            "organization": owner,
            "location": profile_exp.get("location", ""),
            "title": profile_exp.get("title", ""),
            "dates": profile_exp.get("dates", ""),
            "bullets": [{"evidence_ids": [item["id"]], "text": item["text"]} for item in items],
        })

    blocked = set(forbidden_resume_terms())
    project_groups = [x for x in rank_evidence_groups(matrix, "project") if x["name"] not in blocked]
    projects = []
    project_data = {p.get("name"): p for p in load_projects(True)}
    for group in project_groups[:project_limit]:
        owner = group["name"]
        items = [evidence[i] for i in group["evidence_ids"]][:2]
        skills = ", ".join(project_data.get(owner, {}).get("skills", [])[:7])
        projects.append({"name": owner, "stack": skills, "bullets": [{"evidence_ids": [item["id"]], "text": item["text"]} for item in items]})

    profile = load_profile()
    req_text = " ".join(x["normalized"] for x in matrix["requirements"]).lower()
    categories = {"Languages & Frameworks": [], "Data & ML": [], "Infrastructure & Tools": []}
    category_map = {
        "python": "Languages & Frameworks", "java": "Languages & Frameworks", "javascript": "Languages & Frameworks", "sql": "Languages & Frameworks",
        "machine learning": "Data & ML", "scikit-learn": "Data & ML", "retrieval-augmented generation": "Data & ML", "numpy": "Data & ML",
        "fastapi": "Languages & Frameworks", "flask": "Languages & Frameworks", "react": "Languages & Frameworks", "rest apis": "Languages & Frameworks", "graphql": "Languages & Frameworks",
        "sqlite": "Infrastructure & Tools", "postgresql": "Infrastructure & Tools", "docker": "Infrastructure & Tools", "linux": "Infrastructure & Tools",
        "git": "Infrastructure & Tools", "github": "Infrastructure & Tools", "grafanalib": "Infrastructure & Tools",
    }
    # Verified skill pool: confirmed core skills, the stacks of the verified
    # projects on this resume, and skills named in the selected experience
    # bullets. Skills the job asks for come first; the rest still represent
    # verified background instead of being dropped.
    pool = [str(s) for s in profile.get("core_skills", [])]
    pool += [str(s) for p in projects for s in project_data.get(p["name"], {}).get("skills", [])]
    bullet_text = " ".join(b["text"] for e in experiences for b in e["bullets"]).lower()
    pool += [k for k in category_map if re.search(rf"\b{re.escape(k)}\b", bullet_text)]
    canonical = {"javascript": "JavaScript", "react": "React", "graphql": "GraphQL", "grafanalib": "Grafanalib", "linux": "Linux"}
    for skill in pool:
        canonical.setdefault(skill.lower(), skill)
    order = list(category_map)
    ranked = sorted((s for s in dict.fromkeys(x.lower() for x in pool) if s in category_map), key=lambda s: (s not in req_text, order.index(s)))
    for label in ranked:
        categories[category_map[label]].append(canonical[label])
    # Data & ML is shown only when the job asks for any of it, so a one-page
    # SWE resume keeps its project bullets instead of an unrequested line.
    if not any(label in req_text for label in ranked if category_map[label] == "Data & ML"):
        categories["Data & ML"] = []
    categories = {k: ", ".join(dict.fromkeys(v)) for k, v in categories.items() if v}
    return {"experiences": experiences, "projects": projects, "skills": categories, "skill_evidence": {}}


def build_application(*, job: dict, output_dir: Path, plan: dict | None = None, tex_name: str = "resume.tex") -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    matrix = build_requirement_matrix(job)
    plan = plan or auto_plan(job, matrix)
    template_input = "../../templates/jakes_resume.tex" if output_dir.name != "output" else "../templates/jakes_resume.tex"
    original_plan = deepcopy(plan)
    removed_content_ids: list[str] = []
    compaction_stage = 0
    ok = False
    log = ""
    tex_path = output_dir / tex_name
    page_count = None
    original_page_count = None
    for stage in range(4):
        compaction_stage = stage
        tex_path = render_resume_tex(plan, output_path=output_dir / tex_name, template_input=template_input)
        ok, log = compile_tex(tex_path)
        page_count = _pdf_page_count(tex_path.with_suffix(".pdf")) if ok else None
        if stage == 0:
            original_page_count = page_count
        if not ok or page_count == 1:
            break
        if stage >= 3:
            break
        plan, removed = compact_plan(plan, stage + 1)
        removed_content_ids.extend(removed)
        if not removed:
            break
    report_path = output_dir / "requirement_coverage.json"
    write_application_report(job=job, matrix=matrix, plan=plan, output_path=report_path)
    compaction = {
        "original_page_count": original_page_count,
        "compaction_stage": compaction_stage,
        "removed_content_ids": removed_content_ids,
        "final_page_count": page_count,
    }
    (output_dir / "resume_compaction.json").write_text(json.dumps(compaction, indent=2), encoding="utf-8")
    return {"tex": str(tex_path), "pdf": str(tex_path.with_suffix(".pdf")), "report": str(report_path), "compiled": ok, "compile_log": log[-4000:], "compaction": compaction}


def _pdf_page_count(pdf_path: Path) -> int | None:
    if not pdf_path.exists():
        return None
    try:
        output = subprocess.check_output(["pdfinfo", str(pdf_path)], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in output.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    return None


def compact_plan(plan: dict, stage: int) -> tuple[dict, list[str]]:
    """Remove the lowest-ranked evidenced project content in fixed stages."""
    result = deepcopy(plan)
    removed: list[str] = []
    projects = result.get("projects", [])
    if stage == 1:
        for project in reversed(projects):
            if len(project.get("bullets", [])) > 1:
                item = project["bullets"].pop()
                removed.extend(item.get("evidence_ids", []))
                break
    elif stage == 2:
        for project in reversed(projects):
            if len(project.get("bullets", [])) > 1:
                kept, discarded = project["bullets"][:1], project["bullets"][1:]
                project["bullets"] = kept
                for item in discarded: removed.extend(item.get("evidence_ids", []))
                break
    elif stage == 3 and projects:
        discarded = projects.pop()
        for item in discarded.get("bullets", []): removed.extend(item.get("evidence_ids", []))
    return result, removed


def _heading(kind: str, value: dict) -> str:
    if kind == "experience":
        return f"  \\resumeSubheading{{{latex_escape(value['organization'])}}}{{{latex_escape(value['location'])}}}{{{latex_escape(value['title'])}}}{{{latex_escape(value['dates'])}}}"
    return f"  \\resumeProjectHeading{{{latex_escape(value['name'])}}}{{{latex_escape(value['stack'])}}}"


def render_resume_tex(plan: dict, *, output_path: Path, template_input: str = "../../templates/jakes_resume.tex") -> Path:
    profile = load_profile()
    contact = profile.get("contact", {})
    phone = latex_escape(contact.get("phone", ""))
    email = latex_escape(contact.get("email", ""))
    email_url = contact.get("email", "")
    github_url = profile.get("links", {}).get("github", "")
    linkedin_url = profile.get("links", {}).get("linkedin", "")
    education = profile.get("education", {}) or {}
    degree_line = " in ".join(x for x in (education.get("degree", ""), education.get("major", "")) if x)
    graduation = str(education.get("graduation", "")).strip()
    graduation_line = f"Expected {graduation}" if graduation and not graduation.lower().startswith(("expected", "graduated")) else graduation

    def _display(url: str) -> str:
        return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")

    header_parts = [latex_escape(x) for x in (profile.get("location", ""),) if x]
    if phone:
        header_parts.append(phone)
    if email_url:
        header_parts.append(f"\\href{{mailto:{email_url}}}{{{email}}}")
    for url in (github_url, linkedin_url):
        if url:
            header_parts.append(f"\\href{{{url}}}{{{latex_escape(_display(url))}}}")
    lines = [
        r"\documentclass[letterpaper,10pt]{article}",
        f"\\input{{{template_input}}}",
        "", r"\begin{document}", "",
        r"\begin{center}",
        r"  {\fontsize{18pt}{21pt}\selectfont\textbf{" + latex_escape(profile.get("name", "")) + r"}}\\[3pt]",
        "  \\fontsize{8.5pt}{10pt}\\selectfont " + "\\enspace\\textbar\\enspace ".join(header_parts),
        r"\end{center}", "",
        r"\section{Education}", r"\resumeSubHeadingListStart",
        "  \\resumeSubheading{" + latex_escape(education.get("institution", "")) + "}{" + latex_escape(education.get("location", profile.get("location", ""))) + "}{" + latex_escape(degree_line) + "}{" + latex_escape(graduation_line) + "}",
        r"\resumeSubHeadingListEnd", "",
        r"\section{Experience}", r"\resumeSubHeadingListStart",
    ]
    for experience in plan["experiences"]:
        lines += [_heading("experience", experience), r"  \resumeItemListStart"]
        for bullet in experience["bullets"]:
            lines.append(f"    % evidence: {', '.join(bullet['evidence_ids'])}")
            lines.append(f"    \\resumeItem{{{latex_escape(bullet['text'])}}}")
        lines.append(r"  \resumeItemListEnd")
    lines += [r"\resumeSubHeadingListEnd", "", r"\section{Projects}", r"\resumeSubHeadingListStart"]
    for project in plan["projects"]:
        lines += [_heading("project", project), r"  \resumeItemListStart"]
        for bullet in project["bullets"]:
            lines.append(f"    % evidence: {', '.join(bullet['evidence_ids'])}")
            lines.append(f"    \\resumeItem{{{latex_escape(bullet['text'])}}}")
        lines.append(r"  \resumeItemListEnd")
    lines += [r"\resumeSubHeadingListEnd", "", r"\section{Technical Skills}"]
    for category, skills in plan["skills"].items():
        lines.append(f"\\textbf{{{latex_escape(category)}:}} {latex_escape(skills)} \\\\")
    lines += ["", r"\end{document}", ""]
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def write_application_report(*, job: dict, matrix: dict, plan: dict, output_path: Path) -> Path:
    evidence = evidence_by_id()
    selected_ids = {ident for group in plan["experiences"] + plan["projects"] for bullet in group["bullets"] for ident in bullet["evidence_ids"]}
    selected_ids.update(ident for ids in plan.get("skill_evidence", {}).values() for ident in ids)
    rows = requirement_locations(matrix, selected_ids)
    traceable = sum(1 for group in plan["experiences"] + plan["projects"] for bullet in group["bullets"] if all(ident in evidence for ident in bullet["evidence_ids"]))
    total_bullets = sum(len(group["bullets"]) for group in plan["experiences"] + plan["projects"])
    report_job = {key: value for key, value in job.items() if not str(key).startswith("_")}
    report = {
        "job": report_job,
        "retrieval": {"retrieved_at": datetime.now(timezone.utc).isoformat(), "complete_description_used": bool(job.get("description"))},
        "requirements": matrix["requirements"],
        "requirement_matrix": rows,
        "coverage": {**matrix["coverage"], "evidence_traceability": round(traceable / total_bullets, 4) if total_bullets else 0.0, "unsupported_excluded": matrix["coverage"]["unsupported_excluded"]},
        "selected_experiences": [{"organization": x["organization"], "title": x["title"], "evidence_ids": [i for b in x["bullets"] for i in b["evidence_ids"]]} for x in plan["experiences"]],
        "selected_projects": [{"name": x["name"], "reason": "highest evidence overlap with parsed requirements", "evidence_ids": [i for b in x["bullets"] for i in b["evidence_ids"]]} for x in plan["projects"]],
        "included_skills": plan["skills"],
        # Only technical/domain/nice-to-have terms are keyword decisions. Legal,
        # education, and logistics items remain visible in the matrix but are
        # never presented as resume keyword gaps.
        "rejected_keywords": [x["normalized"] for x in matrix["matches"] if x["category"] in {"TECHNICAL_REQUIREMENT", "DOMAIN_SIGNAL", "NICE_TO_HAVE"} and x["match_type"] in {"UNSUPPORTED", "SUPPORTED_ADJACENT"}],
    }
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = [
        "# Requirement Coverage Report", "", f"- Company: {job.get('company')}", f"- Role: {job.get('title')}", f"- URL: {job.get('url')}", f"- Retrieved: {job.get('retrieved_at', 'not supplied')}", "",
        "## Coverage", "", "```json", json.dumps(report["coverage"], indent=2), "```", "",
        "## Requirements", "", "| Category | Priority | Original JD wording | Normalized | Match | Evidence IDs | Resume location | Action |", "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        original = row["original"].replace("|", "\\|")
        markdown.append(f"| {row['category']} | {row['priority']} | {original} | {row['normalized']} | {row['match_type']} | {', '.join(row['resume_evidence_ids']) or '—'} | {row['resume_location']} | {row['action']} |")
    markdown += ["", "## Selected experiences", ""] + [f"- {x['organization']} — {x['title']} ({', '.join(x['evidence_ids'])})" for x in report["selected_experiences"]]
    markdown += ["", "## Selected projects", ""] + [f"- {x['name']}: {x['reason']} ({', '.join(x['evidence_ids'])})" for x in report["selected_projects"]]
    markdown += ["", "## Rejected or removed keywords", "", ", ".join(report["rejected_keywords"]) or "None"]
    output_path.with_suffix(".md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return output_path


def compile_tex(tex_path: Path) -> tuple[bool, str]:
    tectonic = shutil.which("tectonic")
    if not tectonic:
        return False, "tectonic not installed"
    proc = subprocess.run([tectonic, "--keep-logs", "--keep-intermediates", tex_path.name], cwd=tex_path.parent, capture_output=True, text=True)
    return proc.returncode == 0, proc.stdout + proc.stderr
