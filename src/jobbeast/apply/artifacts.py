"""Canonical, hashable application artifacts."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import shutil
from datetime import datetime, timezone


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def promote_resume(*, output_dir: Path, job_id: str, company: str, role: str, source_url: str) -> tuple[Path, Path]:
    candidates = [output_dir / name for name in ("resume_v4.pdf", "resume_v3.pdf", "resume.pdf")]
    source = next((path for path in candidates if path.exists()), None)
    if source is None:
        raise FileNotFoundError(f"No approved resume PDF found in {output_dir}")
    final = output_dir / "resume_final.pdf"
    if source.resolve() != final.resolve():
        shutil.copy2(source, final)
    manifest = output_dir / "resume_manifest.json"
    payload = {
        "job_id": job_id, "company": company, "role": role, "source_url": source_url,
        "resume_sha256": sha256_file(final), "created_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
    }
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return final, manifest
