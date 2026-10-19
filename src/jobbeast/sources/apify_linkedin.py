"""LinkedIn job search results via an Apify actor (default: curious_coder/linkedin-jobs-scraper).

The actor scrapes LinkedIn's public (logged-out) job search pages, so no
LinkedIn credentials or cookies are ever sent. The Apify token is read from the
APIFY_TOKEN environment variable or the repo's gitignored .env file and is only
ever sent in an Authorization header.

Each search query runs as its own actor run: the actor's job counter is shared
across all URLs of one run (limitPerSource caps the whole run, not each URL),
and separate runs let one rate-limited query fail without affecting the others.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

import requests

from ..models import Job

SOURCE = "linkedin"
DEFAULT_ACTOR = "curious_coder/linkedin-jobs-scraper"
API = "https://api.apify.com/v2"
ROOT = Path(__file__).resolve().parents[3]
US_GEO_ID = "103644278"

# Precision of the source's own posting time.
EXACT, DATE_ONLY, UNKNOWN = "EXACT", "DATE_ONLY", "UNKNOWN"
# How the candidate would apply, as far as the source shows.
EXTERNAL, LINKEDIN_EASY_APPLY, LINKEDIN_PAGE_ONLY = "EXTERNAL", "LINKEDIN_EASY_APPLY", "LINKEDIN_PAGE_ONLY"

# LinkedIn's AI search dropped the experience/job-type/sort URL filters, so the
# internship and technical-role constraints are enforced on the results instead.
_INTERNSHIP = re.compile(r"\bintern(?:ship)?s?\b|\bco-?op\b|\bapprentice", re.I)
_TECHNICAL = re.compile(
    r"\bsoftware\b|\bdevelop(?:er|ment engineer)\b|\bprogramm(?:er|ing)\b|\bswe\b|\bsde\b|"
    r"\bback[- ]?end\b|\bfull[- ]?stack\b|\bfront[- ]?end\b|\bweb\b|\bmobile\b|\bios\b|\bandroid\b|"
    r"\binfrastructure\b|\bplatform\b|\b(?:distributed|computer|embedded|software|operating) systems?\b|"
    r"\bcloud\b|\bdevops\b|\bsre\b|\bsite reliability\b|\bfirmware\b|\bembedded\b|\bhpc\b|"
    r"\bdata\b|\bmachine learning\b|\bml\b|\bai\b|\bartificial intelligence\b|\bdeep learning\b|"
    r"\bnlp\b|\bcomputer vision\b|\bllms?\b|\banalytics\b|"
    r"\bqa\b|\bquality assurance\b|\btest(?:ing)? engineer|\bsdet\b|\btest automation\b|\bcomputer science\b",
    re.I,
)
# A bare "Engineering Intern" title says nothing about the discipline; the
# description decides, unless the title names a non-software discipline.
_GENERIC_ENGINEERING = re.compile(r"\bengineer(?:ing)?\b|\brobotics\b", re.I)
_OTHER_DISCIPLINE = re.compile(
    r"\b(?:civil|mechanical|propulsion|chemical|structural|manufacturing|industrial|environmental|"
    r"biomedical|aerospace|process|construction|geotechnical|transportation|petroleum|mining|sales)\b", re.I)
_SOFTWARE_EVIDENCE = re.compile(
    r"\bpython\b|\bjava\b|\bc\+\+|\bjavascript\b|\btypescript\b|\bsql\b|\bsoftware\b|\bprogramming\b|"
    r"\bmachine learning\b|\bros\b|\bapis?\b|\bcloud\b|\bgit\b|\blinux\b", re.I)
_RETRYABLE = {429, 500, 502, 503, 504}


def apify_token() -> str:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "APIFY_TOKEN":
                token = value.strip().strip("'\"")
    if not token or token.startswith("your_"):
        raise RuntimeError(
            "APIFY_TOKEN is not set. The LinkedIn source is optional: either skip it "
            "(use --sources simplify) or add your own Apify token as APIFY_TOKEN=... "
            "in .env (gitignored) or export it."
        )
    return token


def search_url(keywords: str, *, location: str = "United States", window_seconds: int = 3600, geo_id: str | None = US_GEO_ID) -> str:
    """A public LinkedIn job search URL limited to postings from the last `window_seconds`.

    Only filters LinkedIn still honours on AI search are used (date posted,
    location); anything else would be rewritten into the keywords.
    """
    params = {"keywords": keywords, "location": location}
    if geo_id:
        params["geoId"] = geo_id
    params["f_TPR"] = f"r{int(window_seconds)}"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def is_target_role(item: dict[str, Any]) -> bool:
    """An internship (by type, seniority, or title) in a technical role.

    The title decides when it names a technical area; a generic engineering
    title is kept only when the description shows software/data work.
    """
    title = str(item.get("title") or "")
    internship = (_INTERNSHIP.search(title)
                  or "intern" in str(item.get("employmentType") or "").lower()
                  or "intern" in str(item.get("seniorityLevel") or "").lower())
    if not internship:
        return False
    if _TECHNICAL.search(title):
        return True
    if _GENERIC_ENGINEERING.search(title) and not _OTHER_DISCIPLINE.search(title):
        description = str(item.get("descriptionText") or item.get("description") or "")
        return len({m.group(0).lower() for m in _SOFTWARE_EVIDENCE.finditer(description)}) >= 2
    return False


def _posted(item: dict[str, Any]) -> tuple[datetime | None, str, str | None]:
    """(exact posting time or None, precision, raw source value)."""
    stamp = item.get("postedAtTimestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        dt = datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, timezone.utc)
        return dt, EXACT, dt.isoformat()
    raw = str(item.get("postedAt") or item.get("publishedAt") or item.get("datePosted") or "").strip()
    if not raw:
        return None, UNKNOWN, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        # A calendar date says nothing about the hour; never turn it into midnight.
        return None, DATE_ONLY, raw
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)), EXACT, raw
    except ValueError:
        return None, UNKNOWN, raw


def _is_linkedin(url: str) -> bool:
    return (urlparse(url).hostname or "").endswith("linkedin.com")


def _application_route(item: dict[str, Any], apply_url: str) -> tuple[str, bool | None]:
    """(route, easy_apply). Easy Apply is only ever set from positive evidence."""
    if apply_url and not _is_linkedin(apply_url):
        return EXTERNAL, False
    method = str(item.get("applyMethod") or item.get("applyType") or "").lower()
    if item.get("easyApply") is True or "easy" in method or "onsite" in method:
        return LINKEDIN_EASY_APPLY, True
    return LINKEDIN_PAGE_ONLY, None


def normalize(item: dict[str, Any], *, window_minutes: int | None = None) -> Job | None:
    """Map one actor dataset item to a Job; None when it lacks an id or title."""
    link = str(item.get("link") or item.get("jobUrl") or item.get("url") or "")
    job_id = str(item.get("id") or item.get("jobId") or "")
    if not job_id and link:
        found = re.search(r"(?:currentJobId=|/view/(?:[^/?]*-)?)(\d{6,})", link)
        job_id = found.group(1) if found else ""
    title = str(item.get("title") or item.get("jobTitle") or "").strip()
    if not job_id or not title:
        return None
    source_url = f"https://www.linkedin.com/jobs/view/{job_id}"  # stable; tracking params dropped
    apply_url = str(item.get("applyUrl") or item.get("applyLink") or "")
    route, easy_apply = _application_route(item, apply_url)
    posted, precision, raw_posted = _posted(item)
    if precision == EXACT:
        confidence = "HIGH"
    elif window_minutes:
        confidence = "MEDIUM"  # posted within the search window, minute unknown
    else:
        confidence = "LOW"
    location = str(item.get("location") or "").strip()
    return Job(
        source=SOURCE,
        source_id=job_id,
        company=str(item.get("companyName") or item.get("company") or "Unknown").strip(),
        title=title,
        # The employer's own posting is the application target when LinkedIn exposes it.
        url=apply_url if route == EXTERNAL else source_url,
        locations=[location] if location else [],
        description=str(item.get("descriptionText") or item.get("description") or ""),
        date_posted=posted,
        active=True,
        terms=[],
        metadata={
            "source_url": source_url,
            "apply_url": apply_url if route == EXTERNAL else "",
            "application_route": route,
            "easy_apply": easy_apply,
            "source_posted_at": raw_posted,
            "source_posted_precision": precision,
            "source_window_minutes": window_minutes,
            "freshness_confidence": confidence,
            "applicants_count": item.get("applicantsCount"),
            "seniority_level": item.get("seniorityLevel"),
            "employment_type": item.get("employmentType"),
        },
    )


def _classify_log(log: str) -> tuple[int, bool]:
    """(LinkedIn 429 responses seen, whether a request ran out of retries on a 429).

    The actor marks a request that exhausted its retries with "⭕"; a 429 that
    succeeded on retry lost nothing.
    """
    hits = log.count("HTTP Error: 429")
    lost = bool(re.search(r"⭕[^\n]*429", log))
    return hits, lost


def run_query(url: str, *, token: str, actor: str = DEFAULT_ACTOR, limit: int = 25, actor_timeout: int = 180,
              attempts: int = 2, backoff_seconds: float = 20.0, session: Any = None) -> dict[str, Any]:
    """Run the actor for one search URL. Never raises; returns items plus a status.

    Only Apify API errors (5xx/429 from Apify itself) are retried, with bounded
    backoff. LinkedIn rate limiting is retried by the actor; it is recorded, not
    worked around.
    """
    http = session or requests
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"urls": [url], "limitPerSource": int(limit), "scrapeCompany": False, "autoConvertToAiSearch": False}
    error = ""
    for attempt in range(attempts):
        try:
            response = http.post(f"{API}/acts/{actor.replace('/', '~')}/runs", json=payload, headers=headers,
                                 params={"waitForFinish": 60, "timeout": actor_timeout, "memory": 1024}, timeout=90)
            if response.status_code in _RETRYABLE:
                raise requests.HTTPError(f"Apify API HTTP {response.status_code}")
            response.raise_for_status()
            run = response.json()["data"]
            deadline = time.monotonic() + actor_timeout + 60
            while run.get("status") in {"READY", "RUNNING"} and time.monotonic() < deadline:
                run = http.get(f"{API}/actor-runs/{run['id']}", headers=headers, params={"waitForFinish": 60}, timeout=90).json()["data"]
            items = http.get(f"{API}/datasets/{run['defaultDatasetId']}/items", headers=headers, params={"clean": 1}, timeout=90).json()
            items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
            try:
                log = http.get(f"{API}/actor-runs/{run['id']}/log", headers=headers, timeout=60).text
            except Exception:
                log = ""
            hits, lost = _classify_log(log)
            if run.get("status") != "SUCCEEDED" and not items:
                status = "FAILED"
            else:
                status = "RATE_LIMITED" if lost else "SUCCEEDED"
            return {"url": url, "status": status, "run_status": run.get("status"), "items": items, "rate_limit_hits": hits}
        except Exception as exc:  # one query never fails the whole refresh
            error = str(exc)[:300]
            if attempt + 1 < attempts:
                time.sleep(backoff_seconds * (attempt + 1))
    return {"url": url, "status": "FAILED", "run_status": None, "items": [], "rate_limit_hits": 0, "error": error}


def fetch(titles: Iterable[str], *, location: str = "United States", window_seconds: int = 3600,
          limit_per_search: int = 25, actor: str = DEFAULT_ACTOR, concurrency: int = 3,
          session: Any = None, token: str | None = None, **_ignored) -> dict[str, Any]:
    """Run every title search; returns {"jobs": [...target roles...], "stats": {...}}."""
    urls = [search_url(t, location=location, window_seconds=window_seconds) for t in titles]
    token = token or apify_token()
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        results = list(pool.map(lambda u: run_query(u, token=token, actor=actor, limit=limit_per_search, session=session), urls))
    window_minutes = int(window_seconds // 60)
    raw = 0
    listings: dict[str, Job] = {}
    dropped: dict[str, str] = {}
    for result in results:
        for item in result["items"]:
            raw += 1
            job = normalize(item, window_minutes=window_minutes)
            if not job or job.source_id in listings or job.source_id in dropped:
                continue  # one posting can match several searches
            if is_target_role(item):
                listings[job.source_id] = job
            else:
                dropped[job.source_id] = f"{job.company} | {job.title}"
    stats = {
        "queries_attempted": len(results),
        "queries_succeeded": sum(r["status"] == "SUCCEEDED" for r in results),
        "queries_rate_limited": sum(r["status"] == "RATE_LIMITED" for r in results),
        "queries_failed": sum(r["status"] == "FAILED" for r in results),
        "linkedin_429_responses": sum(r["rate_limit_hits"] for r in results),
        "raw_results": raw,
        "unique_listings": len(listings) + len(dropped),
        "off_target_dropped": len(dropped),
        "off_target_examples": sorted(dropped.values())[:10],
        "normalized_jobs": len(listings),
        "queries": [{"url": r["url"], "status": r["status"], "results": len(r["items"]), **({"error": r["error"]} if r.get("error") else {})} for r in results],
    }
    return {"jobs": list(listings.values()), "stats": stats}
