"""Deterministic ATS fingerprinting for official application pages."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from urllib.parse import urlparse
import re


@dataclass(frozen=True)
class Detection:
    provider: str
    confidence: float
    signals: tuple[str, ...]
    canonical_url: str
    official_host_verified: bool
    provider_host: str = ""
    provider_job_id: str = ""
    page_title: str = ""
    form_actions: tuple[str, ...] = ()
    script_domains: tuple[str, ...] = ()
    iframe_domains: tuple[str, ...] = ()
    meta_tags: tuple[str, ...] = ()
    dom_markers: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


HOSTS = {
    "greenhouse": ("greenhouse.io",), "lever": ("lever.co",), "ashby": ("ashbyhq.com",),
    "workday": ("myworkdayjobs.com", "workday.com"), "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",), "phenom": ("phenom.com", "phenompeople.com"),
    "eightfold": ("eightfold.ai",), "successfactors": ("successfactors.com",),
    "oracle_cloud": ("oraclecloud.com",), "taleo": ("taleo.net",),
    "avature": ("avature.net", "avature.com"),
}

MARKERS = {
    "greenhouse": ("greenhouse", "boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se", "gh_jid=", "grnhse_app_new"),
    "lever": ("jobs.lever.co", "lever application", "lever-jobs"),
    "ashby": ("ashbyhq", "jobs.ashbyhq.com", "ashby application"),
    "workday": ("workday", "myworkdayjobs", "wd1.myworkdayjobs"),
    "icims": ("icims", "iCIMS", "icims.com"), "smartrecruiters": ("smartrecruiters", "smart recruiters"),
    "phenom": ("phenom", "phenompeople"), "eightfold": ("eightfold", "eightfold.ai"),
    "successfactors": ("successfactors", "career site powered by sap"),
    "oracle_cloud": ("oraclecloud", "oracle recruiting", "oracle hcm"),
    "taleo": ("taleo", "oracle taleo"),
    "avature": ("avature", "avature.portal.id", "avature.portal"),
}


class _PageFingerprint(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""; self._in_title = False
        self.forms: list[str] = []; self.scripts: list[str] = []; self.iframes: list[str] = []
        self.meta: list[str] = []; self.markers: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs); tag = tag.lower()
        if tag == "title": self._in_title = True
        if tag == "form" and attrs.get("action"): self.forms.append(attrs["action"])
        if tag == "script" and attrs.get("src"): self.scripts.append(attrs["src"])
        if tag == "iframe" and attrs.get("src"): self.iframes.append(attrs["src"])
        if tag == "meta":
            value = " ".join(str(attrs.get(key, "")) for key in ("name", "property", "content")).strip()
            if value: self.meta.append(value)
        for key in ("id", "class", "name", "data-automation-id", "data-qa", "aria-label"):
            if attrs.get(key): self.markers.append(f"{key}={attrs[key]}")

    def handle_endtag(self, tag):
        if tag.lower() == "title": self._in_title = False

    def handle_data(self, data):
        if self._in_title: self.title += data


def _host_matches(host: str, apex: str) -> bool:
    return host == apex or host.endswith("." + apex)


def _domain(value: str) -> str:
    parsed = urlparse(value if re.match(r"^[a-z]+://", value, re.I) else "https://" + value)
    return (parsed.hostname or "").lower().rstrip(".")


def _provider_job_id(provider: str, url: str, html: str) -> str:
    patterns = {
        "greenhouse": (r"(?:/jobs/|gh_jid=)([A-Za-z0-9_-]+)",),
        "lever": (r"/([0-9a-f]{8}-[0-9a-f-]{20,})",), "ashby": (r"/([0-9a-f]{20,})",),
        "icims": (r"/jobs/(\d+)",), "taleo": (r"(?:job|req)(?:id|=)(\d+)",),
    }
    for pattern in patterns.get(provider, (r"(?:jobs|job|requisition|JobDetail)[=/]([A-Za-z0-9_-]+)",)):
        match = re.search(pattern, url, re.I)
        if match: return match.group(1)
    for pattern in patterns.get(provider, ()):
        match = re.search(pattern, html, re.I)
        if match: return match.group(1)
    return ""


def detect_provider(url: str, *, html: str = "", final_url: str | None = None) -> Detection:
    candidate = final_url or url
    parsed = urlparse(candidate); host = (parsed.hostname or "").lower().rstrip(".")
    parser = _PageFingerprint()
    if html:
        try: parser.feed(html)
        except Exception: pass
    title = re.sub(r"\s+", " ", parser.title).strip()
    low = html.lower()
    sources = [host, candidate.lower(), title.lower(), *[x.lower() for x in parser.forms + parser.scripts + parser.iframes + parser.meta + parser.markers]]
    ranked: list[tuple[int, str, list[str]]] = []
    for provider, domains in HOSTS.items():
        evidence: list[str] = []
        if any(_host_matches(host, domain) for domain in domains): evidence.append(f"hostname contains recognized {provider} host")
        for value in parser.forms:
            if any(_host_matches(_domain(value), domain) for domain in domains): evidence.append(f"form action host {_domain(value)}")
        for value in parser.scripts:
            if any(_host_matches(_domain(value), domain) for domain in domains): evidence.append(f"script source host {_domain(value)}")
        for value in parser.iframes:
            if any(_host_matches(_domain(value), domain) for domain in domains): evidence.append(f"iframe source host {_domain(value)}")
        haystack = " ".join(sources)
        hits = []
        for needle in MARKERS.get(provider, ()):
            pattern = re.escape(needle.lower())
            if re.search(pattern, haystack) if any(not char.isalnum() for char in needle) else re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", haystack):
                hits.append(needle)
        evidence.extend(f"DOM/title marker: {hit}" for hit in hits)
        if evidence: ranked.append((len(set(evidence)), provider, list(dict.fromkeys(evidence))))
    if ranked:
        _, provider, evidence = max(ranked, key=lambda item: item[0])
        confidence = min(0.99, 0.80 + 0.05 * max(0, len(evidence) - 1))
        provider_job_id = _provider_job_id(provider, candidate, html) or _provider_job_id(provider, url, html)
        return Detection(provider, confidence, tuple(evidence), candidate, any(_host_matches(host, d) for d in HOSTS[provider]), host, provider_job_id, title, tuple(parser.forms), tuple(_domain(x) for x in parser.scripts), tuple(_domain(x) for x in parser.iframes), tuple(parser.meta), tuple(parser.markers))
    generic_form = bool(re.search(r"<form\b|<input\b|<textarea\b|<select\b|role=[\"']combobox", low, re.I))
    evidence = ["generic application control/form detected"] if generic_form else ["no recognized ATS host, form action, script, iframe, or marker"]
    return Detection("unknown", 0.0, tuple(evidence), candidate, False, host, _provider_job_id("unknown", candidate, html) or _provider_job_id("unknown", url, html), title, tuple(parser.forms), tuple(_domain(x) for x in parser.scripts), tuple(_domain(x) for x in parser.iframes), tuple(parser.meta), tuple(parser.markers))
