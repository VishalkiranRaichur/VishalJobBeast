"""Evidence-bank access and traceability helpers."""
from __future__ import annotations

from pathlib import Path
import yaml

from .profile import PROFILE_DIR


EVIDENCE_PATH = PROFILE_DIR / "evidence.yaml"


def load_evidence(verified_only: bool = True) -> list[dict]:
    if not EVIDENCE_PATH.exists():
        return []
    data = yaml.safe_load(EVIDENCE_PATH.read_text(encoding="utf-8")) or {}
    items = data.get("items", [])
    if verified_only:
        items = [item for item in items if item.get("verified") is True]
    return items


def evidence_by_id(verified_only: bool = True) -> dict[str, dict]:
    return {item["id"]: item for item in load_evidence(verified_only) if item.get("id")}


def evidence_text(item: dict) -> str:
    return " ".join([
        str(item.get("text", "")),
        str(item.get("owner", "")),
        " ".join(str(x) for x in item.get("tags", [])),
    ]).lower()


def validate_evidence_bank() -> list[str]:
    """Return deterministic schema errors instead of silently accepting bad evidence."""
    errors: list[str] = []
    seen: set[str] = set()
    for item in load_evidence(False):
        ident = item.get("id", "")
        if not ident:
            errors.append("evidence item missing id")
        elif ident in seen:
            errors.append(f"duplicate evidence id: {ident}")
        seen.add(ident)
        for required in ("kind", "text", "source", "verified"):
            if required not in item:
                errors.append(f"{ident or '<missing id>'} missing {required}")
    return errors


def evidence_ids_for_owner(owner: str) -> list[str]:
    needle = owner.lower()
    return [item["id"] for item in load_evidence() if needle == str(item.get("owner", "")).lower()]
