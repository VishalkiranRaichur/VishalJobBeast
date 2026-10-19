import json
from pathlib import Path
from urllib.parse import urlparse


def _greenhouse_match(pattern: str, url: str) -> bool:
    parsed = urlparse(url)
    if pattern == "https://job-boards.greenhouse.io/*":
        return parsed.scheme == "https" and parsed.netloc == "job-boards.greenhouse.io"
    return pattern == "https://*.greenhouse.io/*" and parsed.scheme == "https" and parsed.netloc.endswith(".greenhouse.io")


def test_companion_manifest_is_strict_manifest_v3_json():
    path = Path(__file__).parents[1] / "browser_extension" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["name"] == "Job Beast Companion"
    assert manifest["version"]
    assert manifest["background"] == {"service_worker": "background.js"}
    assert (path.parent / "content.js").exists()
    assert (path.parent / manifest["background"]["service_worker"]).exists()
    assert "tabs" in manifest["permissions"]
    assert "http://127.0.0.1:8765/*" in manifest["host_permissions"]
    greenhouse = next(item for item in manifest["content_scripts"] if "content.js" in item["js"])
    assert greenhouse["all_frames"] is True
    assert greenhouse["run_at"] == "document_idle"
    assert "https://job-boards.greenhouse.io/*" in greenhouse["matches"]
    assert "https://*.greenhouse.io/*" in greenhouse["matches"]
    enova = "https://job-boards.greenhouse.io/enova/jobs/8239619"
    assert any(_greenhouse_match(pattern, enova) for pattern in greenhouse["matches"])
