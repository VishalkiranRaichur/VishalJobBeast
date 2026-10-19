from pathlib import Path
import json
from urllib.request import urlopen, Request

from jobbeast.apply.bridge import BridgeRun, build_packet


def test_bridge_serves_active_packet_and_resume(tmp_path):
    resume = tmp_path / "resume_final.pdf"
    resume.write_bytes(b"pdf")
    job = {"id": "job-1", "company": "Astranis", "title": "Intern", "url": "https://job-boards.greenhouse.io/astranis/jobs/1", "canonical_url": "https://job-boards.greenhouse.io/astranis/jobs/1", "ats_provider": "greenhouse"}
    packet = build_packet(job=job, answers=None, resume_path=resume, submit=False)
    assert packet["internal_job_id"] == "job-1"
    assert packet["provider_job_id"] == "1"
    bridge = BridgeRun(packet, tmp_path, port=18765)
    bridge.start()
    try:
        with urlopen("http://127.0.0.1:18765/application/current") as response:
            assert json.loads(response.read())["job_id"] == "job-1"
        with urlopen("http://127.0.0.1:18765/application/job-1/resume") as response:
            assert response.read() == b"pdf"
        diagnostic = Request("http://127.0.0.1:18765/diagnostics", data=b'{"type":"CONTENT_SCRIPT_STARTED"}', headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(diagnostic):
            pass
        assert "CONTENT_SCRIPT_STARTED" in (tmp_path / "browser" / "bridge_diagnostics.jsonl").read_text()
        request = Request("http://127.0.0.1:18765/application/job-1/result", data=b'{"status":"READY_TO_APPLY"}', headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request):
            pass
        assert bridge.wait(timeout=1)["status"] == "READY_TO_APPLY"
    finally:
        bridge.stop()
