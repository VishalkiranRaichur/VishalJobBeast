"""Submission preflight checks. A failed check is never auto-overridden."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import subprocess


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def _page_count(pdf: Path) -> int | None:
    try:
        output = subprocess.check_output(["pdfinfo", str(pdf)], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in output.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    return None


def _text(pdf: Path) -> str:
    try:
        return subprocess.check_output(["pdftotext", str(pdf), "-"], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError):
        return ""


def _validation_ok(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        return not bool(json.loads(path.read_text(encoding="utf-8")).get("errors"))
    except (OSError, ValueError, TypeError):
        return False


def _contact_present(text: str) -> bool:
    """The resume must show the active profile's own email and phone."""
    from ..profile import load_profile
    contact = (load_profile().get("contact") or {})
    email, phone = str(contact.get("email", "")).strip(), str(contact.get("phone", "")).strip()
    digits = lambda s: "".join(ch for ch in s if ch.isdigit())
    return bool(email) and email in text and bool(phone) and digits(phone) in digits(text)


def run_preflight(*, job: dict, output_dir: Path, resume_path: Path, manifest_path: Path, detection: dict | None, unresolved: list[dict] | None = None, duplicate: bool = False, submit: bool = False) -> dict:
    checks = [
        Check("correct_company", bool(job.get("company")), str(job.get("company", ""))),
        Check("correct_role", bool(job.get("title")), str(job.get("title", ""))),
        Check("application_url_present", bool(job.get("url")), str(job.get("url", ""))),
        Check("canonical_resume_exists", resume_path.exists(), str(resume_path)),
        Check("resume_is_one_page", _page_count(resume_path) == 1, str(_page_count(resume_path))),
        Check("resume_manifest_exists", manifest_path.exists(), str(manifest_path)),
        Check("contact_details_present", _contact_present(_text(resume_path)), "profile phone and email extracted from PDF"),
        Check("resume_validation_present", _validation_ok(output_dir / "validation.json"), "existing validation.json has no errors"),
        Check("no_unresolved_questions", not unresolved, f"{len(unresolved or [])} unresolved"),
        Check("no_duplicate_submission", not duplicate, "prior verified submission" if duplicate else "none"),
    ]
    if submit:
        checks.append(Check("submit_explicitly_requested", True, "--submit supplied"))
    result = {"passed": all(check.passed for check in checks), "checks": [check.as_dict() for check in checks], "submit": submit}
    (output_dir / "preflight.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
