"""Application receipts and redacted unresolved-question artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_receipt(output_dir: Path, payload: dict[str, Any]) -> Path:
    return _write(output_dir / "submission.json", {"recorded_at": datetime.now(timezone.utc).isoformat(), **payload})


def write_unresolved(output_dir: Path, questions: list[dict | str], *, reason: str = "NEEDS_INPUT") -> Path:
    safe = []
    for item in questions:
        safe.append(item if isinstance(item, str) else {key: value for key, value in item.items() if key not in {"answer", "value"}})
    return _write(output_dir / "unresolved_questions.json", {"reason": reason, "questions": safe, "recorded_at": datetime.now(timezone.utc).isoformat()})
