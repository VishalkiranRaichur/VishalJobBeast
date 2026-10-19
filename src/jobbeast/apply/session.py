"""Manual security-check watcher for one persistent Job Beast browser session."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .browser import close_browser, open_browser
from ..store import get_job


def signal_paths(root: Path) -> tuple[Path, Path]:
    data = root / "data"
    return data / "browser-session-continue", data / "browser-session-close"


async def run_manual_session(*, job_id: str, root: Path) -> None:
    row = get_job(job_id)
    if not row:
        raise ValueError(f"Unknown job id: {job_id}")
    continue_signal, close_signal = signal_paths(root)
    continue_signal.unlink(missing_ok=True)
    close_signal.unlink(missing_ok=True)
    handle = await open_browser(profile_dir=root / "data" / "browser-profile", headed=True, reuse=True)
    page = handle.page
    try:
        if not page.url.startswith(row["url"]):
            await page.goto(row["url"], wait_until="domcontentloaded", timeout=30000)
        print("CONTROLLED_BROWSER_OPENED", flush=True)
        print(f"CURRENT_URL: {page.url}", flush=True)
        print("Complete the security check manually. Do not submit.", flush=True)
        print(f"After the real form is visible, run: jobbeast browser-session {job_id} --continue", flush=True)
        while not continue_signal.exists() and not close_signal.exists():
            await asyncio.sleep(1)
        if close_signal.exists():
            return
        await page.wait_for_timeout(500)
        output = root / "output" / job_id / "browser"
        output.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": page.url,
            "title": await page.title(),
            "forms": await page.locator("form").count(),
            "file_inputs": await page.locator('input[type="file"]').count(),
            "controls": await page.locator("input, textarea, select").count(),
        }
        (output / "manual_session_dom.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        await page.screenshot(path=str(output / "manual_session.png"), full_page=True)
        print("MANUAL_SESSION_INSPECTED", json.dumps(payload), flush=True)
        print("Browser remains open. The executor can now attach to this same session.", flush=True)
        while not close_signal.exists():
            await asyncio.sleep(1)
    finally:
        await close_browser(handle)


def signal_manual_session(root: Path, action: str) -> Path:
    continue_signal, close_signal = signal_paths(root)
    target = continue_signal if action == "continue" else close_signal
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    return target
