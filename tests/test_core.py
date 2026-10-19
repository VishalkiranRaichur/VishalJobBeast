from datetime import datetime, timezone
import os
from pathlib import Path

from jobbeast.models import Job
from jobbeast.store import connect, upsert_jobs, list_jobs


def test_fingerprint_dedupes_cross_source(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("JOBBEAST_DB", str(db))
    a = Job(source="simplify", source_id="1", company="Acme", title="ML Intern", locations=["San Jose, CA"], url="https://a")
    b = Job(source="indeed", source_id="2", company="Acme", title="ML Intern", locations=["San Jose, CA"], url="https://b")
    assert a.fingerprint == b.fingerprint
    assert upsert_jobs([a]) == (1, 0)
    assert upsert_jobs([b]) == (0, 1)
    assert len(list_jobs()) == 1


def test_different_locations_are_distinct():
    a = Job(source="x", company="Acme", title="SWE Intern", locations=["San Jose, CA"], url="a")
    b = Job(source="x", company="Acme", title="SWE Intern", locations=["New York, NY"], url="b")
    assert a.fingerprint != b.fingerprint
