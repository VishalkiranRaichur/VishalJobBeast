from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import requests

from ..models import Job

DEFAULT_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"


def _parse(payload, season: str, active_only: bool, visible_only: bool) -> list[Job]:
    if not isinstance(payload, list):
        raise ValueError("Simplify listings payload changed shape: expected a list")
    jobs: list[Job] = []
    for item in payload:
        if active_only and not item.get("active", False):
            continue
        if visible_only and item.get("is_visible") is False:
            continue
        terms = item.get("terms") or []
        if season and terms and season not in terms:
            continue
        ts = item.get("date_posted")
        posted = None
        if isinstance(ts, (int, float)) and ts > 0:
            posted = datetime.fromtimestamp(ts, tz=timezone.utc)
        jobs.append(Job(
            source="simplify",
            source_id=str(item.get("id") or ""),
            company=str(item.get("company_name") or "Unknown"),
            title=str(item.get("title") or "Unknown"),
            url=str(item.get("url") or item.get("company_url") or ""),
            locations=[str(x) for x in (item.get("locations") or [])],
            date_posted=posted,
            active=bool(item.get("active", True)),
            terms=[str(x) for x in terms],
            metadata={
                "source_label": item.get("source"),
                "company_url": item.get("company_url"),
                "source_listing_id": str(item.get("id") or ""),
                "date_updated": item.get("date_updated"),
                "source_date_posted": ts,
            },
        ))
    return jobs


def fetch(url: str = DEFAULT_URL, season: str = "Summer 2027", active_only: bool = True, visible_only: bool = True) -> list[Job]:
    response = requests.get(url, timeout=30, headers={"User-Agent": "job-beast/0.1"})
    response.raise_for_status()
    return _parse(response.json(), season, active_only, visible_only)


def fetch_file(path: str | Path, season: str = "Summer 2027", active_only: bool = True, visible_only: bool = True) -> list[Job]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return _parse(payload, season, active_only, visible_only)
