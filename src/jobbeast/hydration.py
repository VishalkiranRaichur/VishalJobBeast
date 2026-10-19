"""Fetch and normalize complete job descriptions from official posting pages."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from datetime import datetime, timezone
import requests

from .apply.detector import detect_provider


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden and data.strip():
            self.parts.append(data.strip())


def _clean(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    # Keep the description bounded and readable while preserving employer wording.
    return "\n".join(dict.fromkeys(lines))[:120_000]


def _json_ld_description(html: str) -> str:
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            payload = json.loads(block.strip())
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict) and item.get("description"):
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(item["description"]))).strip()
    return ""


def fetch_official(url: str, *, timeout: int = 30) -> dict:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "job-beast/0.1"}, allow_redirects=True)
    response.raise_for_status()
    html = response.text
    parser = _VisibleText()
    parser.feed(html)
    visible = _clean("\n".join(parser.parts))
    json_description = _clean(_json_ld_description(html))
    description = json_description if len(json_description) > len(visible) // 3 else visible
    detection = detect_provider(url, html=html, final_url=response.url)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    return {
        "description": description,
        "official_url": url,
        "canonical_url": response.url,
        "ats_provider": detection.provider,
        "official_host_verified": detection.official_host_verified,
        "ats_provider_confidence": detection.confidence,
        "ats_provider_evidence": list(detection.signals),
        "ats_provider_host": detection.provider_host,
        "provider_job_id": detection.provider_job_id,
        "fingerprint": detection.as_dict(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "page_title": title,
        "status": "HYDRATED" if len(description) >= 300 else "INCOMPLETE",
    }
