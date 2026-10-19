from jobbeast.models import Job
from jobbeast.radar import queue_latency_metrics, run_worker
from jobbeast.store import connect, sync_source_jobs, upsert_application_queue


def test_source_sync_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    job = Job(source="simplify", source_id="s-1", company="Example", title="Software Intern", url="https://example.com/1")
    first = sync_source_jobs([job], "simplify")
    second = sync_source_jobs([job], "simplify")
    assert len(first["new"]) == 1
    assert second["new"] == []
    assert second["updated"] == []


def test_worker_continues_after_needs_human(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    jobs = [
        Job(source="simplify", source_id="s-1", company="A", title="Software Intern", url="https://example.com/a"),
        Job(source="simplify", source_id="s-2", company="B", title="Data Intern", url="https://example.com/b"),
    ]
    sync_source_jobs(jobs, "simplify")
    with connect() as con:
        ids = [r["id"] for r in con.execute("select id from jobs order by id")]
    for jid in ids:
        upsert_application_queue(jid, status="QUEUED", lane="BACKLOG", queue_priority=1, fit_score=1, freshness_score=0, eligibility_status="ELIGIBLE")
    calls = []

    def fake_apply(*, job_id, **_kwargs):
        calls.append(job_id)
        return {"job_id": job_id, "state": "NEEDS_HUMAN" if len(calls) == 1 else "SUBMITTED"}

    result = run_worker(limit=2, root=tmp_path, runner=fake_apply)
    assert calls == ids
    assert [item["state"] for item in result] == ["NEEDS_HUMAN", "SUBMITTED"]


def test_queue_metrics_exclude_old_backlog_from_fresh_cohort(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    jobs = [
        Job(source="simplify", source_id="old", company="Old", title="Intern", url="https://example.com/old"),
        Job(source="simplify", source_id="fresh", company="Fresh", title="Intern", url="https://example.com/fresh"),
    ]
    sync_source_jobs(jobs, "simplify")
    with connect() as con:
        con.execute("UPDATE jobs SET source_date_posted=datetime('now','-5 days') WHERE source_id='old'")
        con.execute("UPDATE jobs SET source_date_posted=datetime('now','-2 hours') WHERE source_id='fresh'")
        ids = {r["source_id"]: r["id"] for r in con.execute("SELECT id,source_id FROM jobs")}
    for job_id in ids.values():
        upsert_application_queue(job_id, status="QUEUED", lane="BACKLOG", queue_priority=1, fit_score=1, freshness_score=0, eligibility_status="ELIGIBLE")
    with connect() as con:
        con.execute("UPDATE application_queue SET first_seen_at=datetime('now','-5 days'), queued_at=datetime('now','-4 days') WHERE job_id=?", (ids["old"],))
        con.execute("UPDATE application_queue SET first_seen_at=datetime('now','-2 minutes'), queued_at=datetime('now','-1 minutes') WHERE job_id=?", (ids["fresh"],))
    metrics = queue_latency_metrics()
    assert metrics["all_time"]["count"] == 2
    assert metrics["fresh_12h"]["count"] == 1
    assert metrics["fresh_12h"]["median_discovery_to_queue_minutes"] == 1.0


def test_linkedin_listing_never_replaces_employer_url_and_provenance_is_kept(monkeypatch, tmp_path):
    import json as _json
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    simplify = Job(source="simplify", source_id="s-9", company="Hewlett Packard Enterprise", title="Software Engineer Intern - Summer 2027",
                   url="https://careers.hpe.com/job/123", locations=["Houston, TX"])
    linkedin = Job(source="linkedin", source_id="l-9", company="Hewlett Packard Enterprise Inc.", title="Software Engineer Intern",
                   url="https://www.linkedin.com/jobs/view/9", locations=["Houston, Texas, United States"])
    sync_source_jobs([simplify], "simplify")
    sync_source_jobs([linkedin], "linkedin", close_missing=False)
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs").fetchall()
        listings = con.execute("SELECT source, job_id FROM source_listings").fetchall()
    assert len(rows) == 1 and rows[0]["url"] == "https://careers.hpe.com/job/123"
    assert {l["source"] for l in listings} == {"simplify", "linkedin"} and len({l["job_id"] for l in listings}) == 1
    assert _json.loads(rows[0]["metadata_json"])["sources"] == ["linkedin", "simplify"]


def test_employer_url_upgrades_an_aggregator_job(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    sync_source_jobs([Job(source="linkedin", source_id="l-1", company="Acme", title="Data Intern", url="https://www.linkedin.com/jobs/view/1")], "linkedin", close_missing=False)
    sync_source_jobs([Job(source="simplify", source_id="s-1", company="Acme", title="Data Intern", url="https://jobs.lever.co/acme/1")], "simplify")
    with connect() as con:
        rows = con.execute("SELECT url, source FROM jobs").fetchall()
    assert [(r["url"], r["source"]) for r in rows] == [("https://jobs.lever.co/acme/1", "simplify")]


def test_fresh_weak_match_never_outranks_much_stronger_older_job():
    from jobbeast.radar import queue_placement
    fresh_weak = queue_placement(45.0, {"age_hours": 0.5, "bonus": 15})
    fresh_ok = queue_placement(70.0, {"age_hours": 2.0, "bonus": 12})
    old_strong = queue_placement(86.7, {"age_hours": 175.0, "bonus": 0})
    fresh_strong = queue_placement(75.0, {"age_hours": 0.5, "bonus": 15})
    assert fresh_weak["freshness_score"] == 0 and fresh_weak["lane"] == "BACKLOG"
    assert old_strong["queue_priority"] > fresh_ok["queue_priority"] > fresh_weak["queue_priority"]
    assert fresh_strong["queue_priority"] > old_strong["queue_priority"] and fresh_strong["lane"] == "FAST_LANE"


def test_rerank_demotes_stale_fast_lane(monkeypatch, tmp_path):
    from jobbeast.radar import rerank_queue
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    sync_source_jobs([Job(source="simplify", source_id="s-1", company="A", title="Intern", url="https://example.com/a")], "simplify")
    with connect() as con:
        con.execute("UPDATE jobs SET source_date_posted=datetime('now','-7 days'), date_posted=datetime('now','-7 days')")
        job_id = con.execute("SELECT id FROM jobs").fetchone()["id"]
    upsert_application_queue(job_id, status="FAST_LANE", lane="FAST_LANE", queue_priority=111.7, fit_score=86.7, freshness_score=25, eligibility_status="ELIGIBLE")
    rerank_queue()
    with connect() as con:
        q = con.execute("SELECT * FROM application_queue").fetchone()
    assert (q["lane"], q["status"], q["freshness_score"], q["queue_priority"]) == ("BACKLOG", "QUEUED", 0.0, 86.7)


def test_radar_never_touches_an_application_handed_to_claude(monkeypatch, tmp_path):
    from jobbeast.radar import _process_job
    from jobbeast.store import transition_application
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    sync_source_jobs([Job(source="simplify", source_id="s-1", company="A", title="Intern", url="https://example.com/a")], "simplify")
    with connect() as con:
        job_id = con.execute("SELECT id FROM jobs").fetchone()["id"]
    upsert_application_queue(job_id, status="READY_FOR_CLAUDE", lane="FAST_LANE", queue_priority=90, fit_score=90, freshness_score=0, eligibility_status="ELIGIBLE")
    for state in ("RESUME_READY", "READY_TO_APPLY", "READY_FOR_CLAUDE"):
        transition_application(job_id, state)
    monkeypatch.setattr("jobbeast.radar.fetch_official", lambda url: (_ for _ in ()).throw(AssertionError("must not refetch")))
    assert _process_job(job_id)["status"] == "UNCHANGED_ACTIVE_APPLICATION"
    with connect() as con:
        assert con.execute("SELECT status FROM application_queue").fetchone()["status"] == "READY_FOR_CLAUDE"


def test_off_target_linkedin_jobs_leave_active_lanes(monkeypatch, tmp_path):
    from jobbeast.radar import park_off_target_jobs
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    jobs = [Job(source="linkedin", source_id="m", company="HPE", title="Marketing Intern", url="https://www.linkedin.com/jobs/view/1"),
            Job(source="linkedin", source_id="s", company="HPE", title="Software Engineer Intern", url="https://www.linkedin.com/jobs/view/2")]
    sync_source_jobs(jobs, "linkedin", close_missing=False)
    with connect() as con:
        ids = {r["source_id"]: r["id"] for r in con.execute("SELECT id, source_id FROM jobs")}
    for jid in ids.values():
        upsert_application_queue(jid, status="QUEUED", lane="PRIORITY", queue_priority=50, fit_score=50, freshness_score=0, eligibility_status="ELIGIBLE")
    parked = park_off_target_jobs()
    assert [p["title"] for p in parked] == ["Marketing Intern"]
    with connect() as con:
        q = {r["job_id"]: r for r in con.execute("SELECT * FROM application_queue")}
        assert con.execute("SELECT COUNT(*) FROM source_listings").fetchone()[0] == 2  # history kept
    assert (q[ids["m"]]["status"], q[ids["m"]]["last_error"]) == ("SKIPPED", "OFF_TARGET_SOURCE_FILTER")
    assert q[ids["s"]]["status"] == "QUEUED"


def test_closed_source_listing_leaves_active_lanes(monkeypatch, tmp_path):
    from jobbeast.radar import reconcile_queue_with_applications
    monkeypatch.setenv("JOBBEAST_DB", str(tmp_path / "jobs.db"))
    job = Job(source="simplify", source_id="s-1", company="A", title="Intern", url="https://example.com/a")
    sync_source_jobs([job], "simplify")
    with connect() as con:
        job_id = con.execute("SELECT id FROM jobs").fetchone()["id"]
    upsert_application_queue(job_id, status="QUEUED", lane="BACKLOG", queue_priority=80, fit_score=80, freshness_score=0, eligibility_status="ELIGIBLE")
    sync_source_jobs([], "simplify")  # the listing disappeared from the source
    reconcile_queue_with_applications()
    with connect() as con:
        q = con.execute("SELECT status, last_error FROM application_queue").fetchone()
    assert (q["status"], q["last_error"]) == ("CLOSED", "SOURCE_LISTING_CLOSED")
