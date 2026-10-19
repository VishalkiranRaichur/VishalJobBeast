from datetime import datetime, timedelta, timezone

from jobbeast.models import Job
from jobbeast.radar import effective_freshness, sync_linkedin_and_rank
from jobbeast.sources import apify_linkedin
from jobbeast.store import connect, get_job, sync_source_jobs

DESC = "Software Engineer Intern. Currently pursuing a Bachelor's degree in Computer Science. " * 6


def _item(job_id="4012345678", **extra):
    item = {
        "id": job_id, "link": f"https://www.linkedin.com/jobs/view/swe-intern-at-example-{job_id}?position=3&trackingId=abc",
        "title": "Software Engineer Intern", "companyName": "Example Co", "location": "New York, NY",
        "postedAt": "2026-10-14", "descriptionText": DESC, "employmentType": "Internship", "applicantsCount": "3",
    }
    item.update(extra)
    return item


class FakeApify:
    """Minimal stand-in for the Apify REST API: one run per posted search URL."""

    def __init__(self, items_by_keyword, logs=None, fail_keywords=()):
        self.items_by_keyword, self.logs, self.fail_keywords = items_by_keyword, logs or {}, set(fail_keywords)
        self.runs, self.payloads, self.headers = {}, [], []

    class _R:
        def __init__(self, data=None, status=200, text=""):
            self._data, self.status_code, self.text = data, status, text
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self._data

    def post(self, url, json=None, headers=None, **_):
        self.payloads.append(json)
        self.headers.append(headers)
        keyword = next(k for k in self.items_by_keyword if k.replace(" ", "+") in json["urls"][0])
        if keyword in self.fail_keywords:
            return self._R(status=500)
        run_id = f"run{len(self.runs)}"
        self.runs[run_id] = keyword
        return self._R({"data": {"id": run_id, "status": "SUCCEEDED", "defaultDatasetId": run_id}})

    def get(self, url, **_):
        run_id = url.split("/")[-2]
        if url.endswith("/items"):
            return self._R(self.items_by_keyword[self.runs[run_id]])
        return self._R(text=self.logs.get(self.runs[run_id], ""))


def test_search_url_sends_only_filters_linkedin_honours():
    url = apify_linkedin.search_url("Software Engineer Intern", window_seconds=1800)
    assert "f_TPR=r1800" in url and "keywords=Software+Engineer+Intern" in url and "geoId=103644278" in url
    # Retired filters would be rewritten by the actor into keywords like "most recent internship".
    for retired in ("sortBy", "f_E=", "f_JT="):
        assert retired not in url


def test_date_only_posting_is_not_turned_into_midnight():
    job = apify_linkedin.normalize(_item(), window_minutes=60)
    assert job.date_posted is None and job.posted_iso() is None
    assert job.metadata["source_posted_at"] == "2026-10-14"
    assert job.metadata["source_posted_precision"] == "DATE_ONLY"
    assert job.metadata["source_window_minutes"] == 60
    assert job.metadata["freshness_confidence"] == "MEDIUM"


def test_exact_timestamp_is_kept_with_high_confidence():
    job = apify_linkedin.normalize(_item(postedAtTimestamp=1_791_900_000_000), window_minutes=60)
    assert job.date_posted == datetime.fromtimestamp(1_791_900_000, timezone.utc)
    assert job.metadata["source_posted_precision"] == "EXACT"
    assert job.metadata["freshness_confidence"] == "HIGH"


def test_date_only_freshness_uses_first_seen_within_window(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    job = apify_linkedin.normalize(_item(), window_minutes=60)
    sync_source_jobs([job], "linkedin", close_missing=False)
    with connect() as con:
        con.execute("UPDATE jobs SET first_seen=datetime('now','-10 minutes')")
        row = con.execute("SELECT * FROM jobs").fetchone()
    fresh = effective_freshness(row)
    assert fresh["basis"] == "FIRST_SEEN_WITHIN_SOURCE_WINDOW"
    assert fresh["bucket"] == "ULTRA_FRESH" and fresh["confidence"] == "MEDIUM"
    assert fresh["age_hours"] < 0.5  # not the ~hours-since-midnight of the date-only value
    assert 1.0 < fresh["max_age_hours"] < 1.5  # at most window + time since first seen


def test_date_only_without_window_gets_no_fresh_advantage(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    sync_source_jobs([apify_linkedin.normalize(_item())], "linkedin", close_missing=False)
    with connect() as con:
        row = con.execute("SELECT * FROM jobs").fetchone()
    assert effective_freshness(row)["bucket"] == "NORMAL"


def test_external_apply_url_is_the_application_target():
    job = apify_linkedin.normalize(_item(applyUrl="https://boards.greenhouse.io/example/jobs/123"))
    assert job.url == "https://boards.greenhouse.io/example/jobs/123"
    assert job.metadata["application_route"] == "EXTERNAL" and job.metadata["easy_apply"] is False
    assert job.metadata["source_url"] == "https://www.linkedin.com/jobs/view/4012345678"


def test_missing_apply_url_is_not_assumed_easy_apply():
    job = apify_linkedin.normalize(_item())
    assert job.metadata["application_route"] == "LINKEDIN_PAGE_ONLY"
    assert job.metadata["easy_apply"] is None
    assert job.url == job.metadata["source_url"] == "https://www.linkedin.com/jobs/view/4012345678"


def test_easy_apply_only_from_positive_evidence():
    job = apify_linkedin.normalize(_item(applyMethod="ComplexOnsiteApply"))
    assert job.metadata["application_route"] == "LINKEDIN_EASY_APPLY" and job.metadata["easy_apply"] is True


def test_normalize_skips_items_without_id_or_title():
    assert apify_linkedin.normalize({"title": "Intern"}) is None
    assert apify_linkedin.normalize({"id": "1"}) is None


def test_target_role_filter_keeps_technical_internships_only():
    keep = ["Software Engineer Intern", "HPC – AI Networking Intern", "Quality Assurance Test Engineer Intern",
            "SSD Firmware Development Engineer Intern", "Analytics and AI Solutions Intern", "Data Analyst Intern",
            "Backend Developer Co-op", "Machine Learning Engineer, Internship"]
    drop = ["Marketing Intern", "Intern, Sales - Foodservice & On-Premise", "Legislative Performance Audit Intern",
            "2027 Undergraduate Summer Internship - Planning", "Account Operations Management Intern."]
    for title in keep:
        assert apify_linkedin.is_target_role({"title": title}), title
    for title in drop:
        assert not apify_linkedin.is_target_role({"title": title}), title
    # Generic engineering titles: the description decides; other disciplines never pass.
    software = "Build tools in Python and SQL; ship software to production."
    assert apify_linkedin.is_target_role({"title": "Engineering Intern", "descriptionText": software})
    assert not apify_linkedin.is_target_role({"title": "Engineering Intern", "descriptionText": "Assist with site surveys and drafting."})
    assert not apify_linkedin.is_target_role({"title": "Civil Engineering Intern", "descriptionText": software})
    assert not apify_linkedin.is_target_role({"title": "Propulsion Engineer Internship", "descriptionText": software})
    # Technical but not an internship (by title or employment type).
    assert not apify_linkedin.is_target_role({"title": "Machine Learning Researcher", "employmentType": "Full-time"})
    assert apify_linkedin.is_target_role({"title": "Machine Learning Researcher", "employmentType": "Internship"})


def test_fetch_runs_each_query_separately_and_dedupes():
    api = FakeApify({"Software Engineer Intern": [_item(), _item("4099999999", title="Marketing Intern")],
                     "Backend Engineer Intern": [_item()]})
    out = apify_linkedin.fetch(["Software Engineer Intern", "Backend Engineer Intern"], session=api, token="test-token")
    assert [j.source_id for j in out["jobs"]] == ["4012345678"]
    s = out["stats"]
    assert (s["queries_attempted"], s["queries_succeeded"], s["raw_results"], s["off_target_dropped"], s["normalized_jobs"]) == (2, 2, 3, 1, 1)
    assert all(len(p["urls"]) == 1 and p["autoConvertToAiSearch"] is False for p in api.payloads)
    assert all(h == {"Authorization": "Bearer test-token"} for h in api.headers)
    assert "test-token" not in repr(s)


def test_rate_limited_or_failed_query_does_not_fail_the_refresh(monkeypatch):
    monkeypatch.setattr(apify_linkedin.time, "sleep", lambda *_: None)
    lost = "\t- ⚠️ Error: HTTP Error: 429\n\t- ⭕️ Error: HTTP Error: 429\n"
    api = FakeApify({"Software Engineer Intern": [_item()], "Data Engineer Intern": [_item("4011111111", title="Data Engineer Intern")],
                     "AI Engineer Intern": []},
                    logs={"Data Engineer Intern": lost}, fail_keywords={"AI Engineer Intern"})
    out = apify_linkedin.fetch(["Software Engineer Intern", "Data Engineer Intern", "AI Engineer Intern"], session=api, token="t")
    s = out["stats"]
    assert (s["queries_attempted"], s["queries_succeeded"], s["queries_rate_limited"], s["queries_failed"]) == (3, 1, 1, 1)
    assert len(out["jobs"]) == 2  # partial results from the rate-limited query are kept
    assert sum(len(p["urls"]) for p in api.payloads) == 4  # failed query retried once (bounded)


def test_retried_429_without_data_loss_is_not_rate_limited():
    api = FakeApify({"Software Engineer Intern": [_item()]}, logs={"Software Engineer Intern": "\t- ⚠️ Error: HTTP Error: 429\n"})
    s = apify_linkedin.fetch(["Software Engineer Intern"], session=api, token="t")["stats"]
    assert s["queries_succeeded"] == 1 and s["queries_rate_limited"] == 0 and s["linkedin_429_responses"] == 1


def test_incremental_sync_does_not_close_older_listings(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    first = Job(source="linkedin", source_id="a", company="A", title="Intern", url="https://example.com/a")
    second = Job(source="linkedin", source_id="b", company="B", title="Intern", url="https://example.com/b")
    sync_source_jobs([first], "linkedin", close_missing=False)
    delta = sync_source_jobs([second], "linkedin", close_missing=False)
    assert delta["closed"] == 0
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 2


def _fetched(jobs, raw=None):
    return {"jobs": jobs, "stats": {"raw_results": raw if raw is not None else len(jobs), "off_target_dropped": 0}}


def test_fresh_date_only_technical_job_enters_fast_lane_without_refetch(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr("jobbeast.radar.fetch_official", lambda url: (_ for _ in ()).throw(AssertionError("no refetch")))
    monkeypatch.setattr("jobbeast.radar.score_job", lambda row: {"score": 82.0})
    job = apify_linkedin.normalize(_item(), window_minutes=60)
    result = sync_linkedin_and_rank(cfg={"titles": ["x"]}, fetcher=lambda *a, **k: _fetched([job]))
    processed = result["processed"][0]
    assert processed["status"] == "FAST_LANE", processed
    assert processed["freshness"]["basis"] == "FIRST_SEEN_WITHIN_SOURCE_WINDOW"
    assert result["fresh_counts"]["<=1h"] == 1
    assert (result["fetch"]["new"], result["fetch"]["distinct_jobs"], result["fetch"]["duplicates"]) == (1, 1, 0)
    row = get_job(processed["job_id"])
    assert row["hydration_status"] == "HYDRATED" and row["official_host_verified"] == 0
    assert row["source_date_posted"] is None


def test_reposts_and_cross_source_duplicates_are_counted(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    simplify = Job(source="simplify", source_id="s-1", company="Example Co", title="Software Engineer Intern",
                   url="https://boards.greenhouse.io/example/jobs/123", locations=["New York, NY"])
    sync_source_jobs([simplify], "simplify")
    same_as_simplify = apify_linkedin.normalize(_item("1"), window_minutes=60)
    repost_a = apify_linkedin.normalize(_item("2", title="Data Engineer Intern"), window_minutes=60)
    repost_b = apify_linkedin.normalize(_item("3", title="Data Engineer Intern"), window_minutes=60)
    result = sync_linkedin_and_rank(cfg={"titles": ["x"]}, fetcher=lambda *a, **k: _fetched([same_as_simplify, repost_a, repost_b]))
    f = result["fetch"]
    assert (f["distinct_jobs"], f["new"], f["duplicates"], f["cross_source_duplicates"]) == (2, 1, 1, 1)


def test_scorer_freshness_uses_first_seen_for_window_constrained_date_only(monkeypatch, tmp_path):
    from jobbeast.scorer import score_job
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    windowed = apify_linkedin.normalize(_item("1"), window_minutes=60)
    unwindowed = apify_linkedin.normalize(_item("2", title="Data Engineer Intern", descriptionText=DESC), window_minutes=None)
    sync_source_jobs([windowed, unwindowed], "linkedin", close_missing=False)
    with connect() as con:
        rows = {r["source_id"]: r for r in con.execute("SELECT * FROM jobs")}
    assert score_job(rows["1"])["freshness_days"] == 0.0
    assert score_job(rows["2"])["freshness_days"] is None


def test_fresh_listings_are_ranked_before_backlog_even_without_posting_time(monkeypatch, tmp_path):
    # Real regression: a 2026-10-14 live fetch left both new LinkedIn jobs
    # "deferred by radar processing limit" because date-less jobs sorted after
    # a 100+ job Simplify backlog.
    from jobbeast.store import upsert_application_queue
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setattr("jobbeast.radar.fetch_official", lambda url: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr("jobbeast.radar.score_job", lambda row: {"score": 80.0})
    backlog = [Job(source="simplify", source_id=f"b{i}", company=f"B{i}", title="Intern", url=f"https://example.com/b{i}",
                   date_posted=datetime.now(timezone.utc) - timedelta(days=2)) for i in range(5)]
    sync_source_jobs(backlog, "simplify")
    with connect() as con:
        rows = con.execute("SELECT id, first_seen FROM jobs").fetchall()
    for r in rows:
        upsert_application_queue(r["id"], status="RANKED", lane="BACKLOG", queue_priority=0, fit_score=None, freshness_score=None, eligibility_status="PENDING", first_seen_at=r["first_seen"])
    job = apify_linkedin.normalize(_item(), window_minutes=60)
    result = sync_linkedin_and_rank(max_process=2, cfg={"titles": ["x"]}, fetcher=lambda *a, **k: _fetched([job]))
    assert result["processed"][0]["job_id"] == result["job_ids"][0]
    assert result["processed"][0]["status"] == "FAST_LANE"
