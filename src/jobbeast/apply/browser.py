"""Controlled Playwright browser lifecycle and same-session attachment."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import urllib.request
from typing import Any


class BrowserUnavailable(RuntimeError):
    pass


@dataclass
class BrowserHandle:
    manager: Any
    browser: Any
    context: Any
    page: Any
    attached: bool
    process: asyncio.subprocess.Process | None
    session_path: Path
    endpoint: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_session(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("endpoint") else None
    except (OSError, ValueError, TypeError):
        return None


def _write_session(path: Path, *, endpoint: str, profile_dir: Path, pid: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "endpoint": endpoint, "profile_dir": str(profile_dir), "pid": pid,
        "created_at": datetime.now(timezone.utc).isoformat(), "owner": "jobbeast",
    }, indent=2), encoding="utf-8")


async def _endpoint_ready(endpoint: str) -> bool:
    def check() -> bool:
        try:
            with urllib.request.urlopen(endpoint + "/json/version", timeout=1.0) as response:
                return response.status == 200
        except Exception:
            return False
    return await asyncio.to_thread(check)


async def _wait_endpoint(endpoint: str, timeout_ms: int) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await _endpoint_ready(endpoint):
            return
        await asyncio.sleep(0.1)
    raise BrowserUnavailable(f"Controlled browser did not expose localhost CDP endpoint within {timeout_ms}ms: {endpoint}")


async def _connect(manager, endpoint: str, timeout_ms: int):
    await _wait_endpoint(endpoint, timeout_ms)
    try:
        browser = await manager.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
    except Exception as exc:
        raise BrowserUnavailable(f"Could not attach to controlled browser at {endpoint}: {exc}") from exc
    if not browser.contexts:
        raise BrowserUnavailable("Controlled browser connected without a browser context")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    return browser, context, page


async def open_browser(*, profile_dir: Path, headed: bool = True, timeout_ms: int = 30000, reuse: bool = True) -> BrowserHandle:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable("Install the optional apply dependencies: pip install -e '.[apply]' and playwright install chromium") from exc
    profile_dir.mkdir(parents=True, exist_ok=True)
    session_path = profile_dir.parent / "browser-session.json"
    manager = await async_playwright().start()
    if reuse:
        existing = _read_session(session_path)
        if existing and await _endpoint_ready(str(existing["endpoint"])):
            try:
                browser, context, page = await _connect(manager, str(existing["endpoint"]), timeout_ms)
                context.set_default_timeout(timeout_ms)
                context.set_default_navigation_timeout(timeout_ms)
                return BrowserHandle(manager, browser, context, page, True, None, session_path, str(existing["endpoint"]))
            except BrowserUnavailable:
                pass
    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"
    executable = manager.chromium.executable_path
    args = [f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}", "--no-first-run", "--no-default-browser-check", "--disable-popup-blocking"]
    if not headed:
        args.append("--headless=new")
    process = await asyncio.create_subprocess_exec(executable, *args, "about:blank")
    try:
        browser, context, page = await _connect(manager, endpoint, timeout_ms)
    except Exception:
        process.terminate()
        await process.wait()
        await manager.stop()
        raise
    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(timeout_ms)
    _write_session(session_path, endpoint=endpoint, profile_dir=profile_dir, pid=process.pid)
    return BrowserHandle(manager, browser, context, page, False, process, session_path, endpoint)


async def close_browser(handle: BrowserHandle, *, keep_session: bool = False) -> None:
    """Disconnect an attached client; terminate only a browser this call owns."""
    try:
        if not handle.attached and not keep_session:
            try:
                await handle.browser.close()
            except Exception:
                pass
            if handle.process:
                try:
                    await asyncio.wait_for(handle.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    handle.process.terminate()
                    await handle.process.wait()
    finally:
        try:
            current = _read_session(handle.session_path)
            if current and current.get("endpoint") == handle.endpoint and not keep_session:
                handle.session_path.unlink(missing_ok=True)
        finally:
            await handle.manager.stop()
