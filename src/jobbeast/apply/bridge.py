"""Loopback-only bridge for the Job Beast Companion Chrome extension."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import time
import re
from typing import Any
from urllib.parse import unquote, urlparse

from ..profile import load_profile


HOST = "127.0.0.1"
PORT = 8765


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _open_normal_browser(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform.startswith("win"):
        subprocess.Popen(["cmd", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _provider_job_id(job: dict[str, Any]) -> str:
    explicit = str(job.get("requisition_id") or "").strip()
    if explicit:
        return explicit
    url = str(job.get("canonical_url") or job.get("official_url") or job.get("url") or "")
    match = re.search(r"(?:jobs|gh_jid|job_app(?:\?token=)?)[=/](\d+)", url, re.I)
    if match:
        return match.group(1)
    return ""


@dataclass
class BridgeRun:
    packet: dict[str, Any]
    output_dir: Path
    host: str = HOST
    port: int = PORT

    def __post_init__(self) -> None:
        self._result: dict[str, Any] | None = None
        self._event = threading.Event()
        self._events_path = self.output_dir / "browser" / "bridge_events.jsonl"
        self._result_path = self.output_dir / "browser" / "bridge_result.json"
        self._diagnostics_path = self.output_dir / "browser" / "bridge_diagnostics.jsonl"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.output_dir.joinpath("browser").mkdir(parents=True, exist_ok=True)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "JobBeastBridge/1.0"

            def log_message(self, *_args: Any) -> None:
                return

            def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
                body = payload if isinstance(payload, bytes) else _json_bytes(payload)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._send(204, b"")

            def do_GET(self) -> None:  # noqa: N802
                path = unquote(urlparse(self.path).path)
                if path == "/health":
                    self._send(200, {"ok": True, "job_id": owner.packet["job_id"]})
                    return
                if path in {"/application/current", f"/application/{owner.packet['job_id']}"}:
                    self._send(200, owner.packet)
                    return
                if path == f"/application/{owner.packet['job_id']}/resume":
                    resume = Path(owner.packet["resume"]["path"])
                    if not resume.exists():
                        self._send(404, {"error": "resume not found"})
                    else:
                        self._send(200, resume.read_bytes(), "application/pdf")
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = unquote(urlparse(self.path).path)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid JSON"})
                    return
                if path in {"/diagnostics", "/events"}:
                    with owner._diagnostics_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    self._send(202, {"accepted": True})
                    return
                if path == f"/application/{owner.packet['job_id']}/events":
                    with owner._events_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    self._send(202, {"accepted": True})
                    return
                if path == f"/application/{owner.packet['job_id']}/result":
                    screenshot_data = payload.pop("screenshot_data", None)
                    if isinstance(screenshot_data, str) and screenshot_data.startswith("data:image/png;base64,"):
                        screenshot = owner.output_dir / "browser" / "submission_confirmation.png"
                        screenshot.write_bytes(base64.b64decode(screenshot_data.split(",", 1)[1]))
                        payload["screenshot"] = str(screenshot)
                    owner._result = payload
                    owner._result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                    owner._event.set()
                    self._send(202, {"accepted": True})
                    return
                self._send(404, {"error": "not found"})

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            raise RuntimeError(f"Job Beast bridge cannot bind {self.host}:{self.port}: {exc}") from exc
        self._thread = threading.Thread(target=self._server.serve_forever, name="jobbeast-bridge", daemon=True)
        self._thread.start()

    def open_application(self) -> None:
        _open_normal_browser(self.packet["official_application_url"])

    def wait(self, timeout: float = 900.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._event.wait(timeout=min(1.0, deadline - time.monotonic())):
                return self._result or {}
        return {
            "status": "NEEDS_HUMAN",
            "reason": "Chrome extension did not report a result before the bridge timeout",
            "job_id": self.packet["job_id"],
        }

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def build_packet(*, job: dict[str, Any], answers: Any, resume_path: Path, submit: bool, duplicate: bool = False) -> dict[str, Any]:
    profile = load_profile()
    flattened = answers.flattened() if answers else {}
    voluntary_eeo = {}
    for category in ("gender", "hispanic_latino", "race", "race_detail", "veteran_status", "disability_status"):
        value_key = f"voluntary_eeo.{category}.value"
        confirmed_key = f"voluntary_eeo.{category}.confirmed"
        value = flattened.get(value_key)
        if flattened.get(confirmed_key) is True and value not in {None, "", "UNKNOWN", "REQUIRES_USER_INPUT", "TODO"}:
            voluntary_eeo[category] = "No" if value is False else "Yes" if value is True else value
    # Keep the values limited to the local bridge packet; they are never
    # written to public artifacts or sent to a third-party service by Job Beast.
    flattened = {key: value for key, value in flattened.items() if not key.startswith("voluntary_eeo.")}
    sources = {}
    if answers:
        for key in flattened:
            found = answers.known(key)
            if found:
                sources[key] = {"source": found.source, "verified": found.verified, "sensitive": found.sensitive}
    return {
        "job_id": job["id"],
        "internal_job_id": job["id"],
        "provider_job_id": _provider_job_id(job),
        "company": job["company"],
        "role": job["title"],
        "ats_provider": job.get("ats_provider") or "greenhouse",
        "official_application_url": job.get("canonical_url") or job["url"],
        "candidate_profile": {
            "identity": profile.get("identity", {}),
            "contact": profile.get("contact", {}),
            "links": profile.get("links", {}),
        },
        "answers": flattened,
        "answer_sources": sources,
        "voluntary_demographics_policy": "confirmed_exact_or_decline_or_blank",
        "voluntary_eeo": voluntary_eeo,
        "resume": {"path": str(resume_path), "filename": resume_path.name},
        "submission_authorized": bool(submit),
        "dry_run": not submit,
        "duplicate": bool(duplicate),
    }
