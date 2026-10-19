from __future__ import annotations

import os
from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[2]


def _resolve_profile_dir() -> Path:
    """Locate the active candidate profile.

    Precedence: JOBBEAST_PROFILE_DIR (any path), then JOBBEAST_PROFILE (a
    directory name under profiles/), then the bundled fictional example.
    """
    explicit = os.environ.get("JOBBEAST_PROFILE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    name = os.environ.get("JOBBEAST_PROFILE", "").strip() or "example"
    return ROOT / "profiles" / name


PROFILE_DIR = _resolve_profile_dir()


def load_profile() -> dict:
    with open(PROFILE_DIR / "profile.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_projects(verified_only: bool = True) -> list[dict]:
    projects = []
    for path in sorted((PROFILE_DIR / "projects").glob("*.yaml")):
        with open(path, "r", encoding="utf-8") as f:
            obj = yaml.safe_load(f) or {}
        obj["_path"] = str(path)
        if verified_only and not obj.get("verified", False):
            continue
        projects.append(obj)
    return projects


def evidence_skill_set() -> set[str]:
    profile = load_profile()
    skills = {str(x).strip().lower() for x in profile.get("core_skills", []) if x}
    for project in load_projects(True):
        skills.update(str(x).strip().lower() for x in project.get("skills", []) if x)
    return skills


def candidate_name(profile: dict | None = None) -> str:
    profile = profile if profile is not None else load_profile()
    return str(profile.get("name", "")).strip()


def required_resume_terms(profile: dict | None = None) -> list[str]:
    """Terms every generated resume must contain: the candidate's name plus section headings."""
    name = candidate_name(profile)
    return ([name] if name else []) + ["Education", "Experience", "Projects", "Technical Skills"]


def forbidden_resume_terms(profile: dict | None = None) -> list[str]:
    """Profile-declared terms plus unverified project names: none may reach a resume."""
    profile = profile if profile is not None else load_profile()
    declared = [str(x) for x in profile.get("forbidden_resume_terms", []) or [] if str(x).strip()]
    return declared + [name for name in unverified_project_names() if name not in declared]


def unverified_project_names() -> list[str]:
    return [str(p.get("name")) for p in load_projects(False) if not p.get("verified") and p.get("name")]
