"""Provider contracts for deterministic ATS adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class FormQuestion:
    key: str
    text: str
    required: bool = False
    control_type: str = "text"
    options: list[str] = field(default_factory=list)


@dataclass
class ProviderResult:
    status: str
    questions: list[FormQuestion] = field(default_factory=list)
    confirmation_text: str = ""
    current_url: str = ""
    screenshot: Path | None = None
    details: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    async def fill(self, page: Any, *, job: Any, resume_path: Path, answers: Any, submit: bool, output_dir: Path) -> ProviderResult: ...

