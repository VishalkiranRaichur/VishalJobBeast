"""Candidate answer-vault loading and safe value resolution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from copy import deepcopy
import yaml


UNKNOWN_TOKENS = {None, "", "REQUIRES_USER_INPUT", "UNKNOWN", "TODO"}


@dataclass(frozen=True)
class Answer:
    value: Any
    source: str
    verified: bool = True
    sensitive: bool = False


@dataclass
class AnswerVault:
    path: Path
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "AnswerVault":
        if not path.exists():
            raise FileNotFoundError(f"Application answer vault not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("Application answer vault must contain a YAML mapping")
        return cls(path=path, data=data)

    def overlay(self, path: Path, *, section: str = "answers") -> "AnswerVault":
        """Return this vault with a job-specific private answer section overlaid."""
        if not path.exists():
            return self
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return self
        overlay_data = payload.get(section)
        # The reusable private vault is intentionally a root mapping, while
        # employer packets use an `answers:` wrapper. Keep both formats
        # private and explicit rather than silently dropping confirmed data.
        if overlay_data is None and path.name == "answers.private.yaml":
            overlay_data = payload
        if not isinstance(overlay_data, dict):
            return self

        def merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(base)
            for key, value in extra.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key] = merge(result[key], value)
                else:
                    result[key] = deepcopy(value)
            return result

        return AnswerVault(path=path, data=merge(self.data, overlay_data))

    def get(self, key: str) -> Answer | None:
        value: Any = self.data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        sensitive = any(token in key.lower() for token in ("authorization", "sponsorship", "salary", "demographic", "gender", "ethnicity", "veteran", "disability"))
        return Answer(value=value, source=f"{self.path.name}:{key}", sensitive=sensitive)

    def flattened(self) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def walk(prefix: str, value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(f"{prefix}.{key}" if prefix else str(key), child)
            else:
                result[prefix] = value

        walk("", self.data)
        return result

    def unknowns(self) -> list[str]:
        return [key for key, value in self.flattened().items() if value in UNKNOWN_TOKENS]

    def known(self, key: str) -> Answer | None:
        answer = self.get(key)
        if answer is None or answer.value in UNKNOWN_TOKENS:
            return None
        return answer

    def prompt_block(self) -> str:
        lines = []
        for key, value in sorted(self.flattened().items()):
            if value in UNKNOWN_TOKENS:
                lines.append(f"- {key}: UNKNOWN — stop and request input if required")
            else:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines)
