from pathlib import Path


def test_companion_rejects_stale_greenhouse_job_tabs():
    source = (Path(__file__).parents[1] / "browser_extension" / "content.js").read_text()
    assert "targetJobId" in source
    assert "currentJobId" in source
    assert "targetJobId !== currentJobId" in source
    assert "sameAtsFamily" in source
