from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any


def _norm(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    description: str = ""
    date_posted: datetime | None = None
    active: bool = True
    terms: list[str] = field(default_factory=list)
    source_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fingerprint(self) -> str:
        # Cross-source semantic duplicate key. Location is included so legitimate
        # separate location postings can coexist.
        loc = "|".join(sorted(_norm(x) for x in self.locations))
        raw = f"{_norm(self.company)}|{_norm(self.title)}|{loc}"
        return sha256(raw.encode()).hexdigest()[:20]

    @property
    def id(self) -> str:
        if self.source_id:
            raw = f"{self.source}:{self.source_id}"
        else:
            raw = f"{self.source}:{self.url}:{self.fingerprint}"
        return sha256(raw.encode()).hexdigest()[:16]

    def posted_iso(self) -> str | None:
        if not self.date_posted:
            return None
        dt = self.date_posted
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    def retrieved_iso(self) -> str:
        dt = self.retrieved_at
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
