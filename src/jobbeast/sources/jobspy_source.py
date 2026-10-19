from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..models import Job


def fetch(query: str, location: str = "", sites: Iterable[str] = ("indeed", "google", "zip_recruiter", "glassdoor"),
          results: int = 30, hours_old: int | None = 168, country: str = "usa") -> list[Job]:
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        raise RuntimeError("JobSpy is optional. Run: pip install -r requirements-jobspy.txt") from exc

    df = scrape_jobs(
        site_name=list(sites),
        search_term=query,
        google_search_term=(f"{query} jobs {location} since last week" if location else query),
        location=location or None,
        results_wanted=results,
        hours_old=hours_old,
        country_indeed=country,
        verbose=0,
    )
    jobs: list[Job] = []
    for _, row in df.fillna("").iterrows():
        date = row.get("date_posted")
        posted = None
        if date:
            try:
                posted = datetime.fromisoformat(str(date))
                if not posted.tzinfo:
                    posted = posted.replace(tzinfo=timezone.utc)
            except Exception:
                posted = None
        jobs.append(Job(
            source=str(row.get("site") or "jobspy"),
            source_id=str(row.get("id") or row.get("job_url") or ""),
            company=str(row.get("company") or row.get("company_name") or "Unknown"),
            title=str(row.get("title") or "Unknown"),
            url=str(row.get("job_url_direct") or row.get("job_url") or ""),
            locations=[str(row.get("location"))] if row.get("location") else [],
            description=str(row.get("description") or ""),
            date_posted=posted,
            active=True,
            terms=[],
            metadata={"job_type": str(row.get("job_type") or "")},
        ))
    return jobs
