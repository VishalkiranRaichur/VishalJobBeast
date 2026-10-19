"""Small local, dependency-free HTML view of the application pipeline."""
from __future__ import annotations

from html import escape
from pathlib import Path

from .store import application_rows


def write_dashboard(path: Path) -> Path:
    rows = application_rows()
    body = []
    for row in rows:
        state = row["state"] or row["status"]
        body.append(
            "<tr>"
            f"<td>{escape(str(row['company']))}</td>"
            f"<td>{escape(str(row['title']))}</td>"
            f"<td>{escape(str(row['fit_score'] or ''))}</td>"
            f"<td class=state-{escape(str(state).lower())}>{escape(str(state))}</td>"
            f"<td>{escape(str(row['needs_human_reason'] or ''))}</td>"
            f"<td><a href='{escape(str(row['url']))}'>posting</a></td>"
            "</tr>"
        )
    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Job Beast Dashboard</title>
<style>body{{font:14px system-ui;margin:2rem;background:#f7f7f8;color:#222}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:.65rem;border:1px solid #ddd;text-align:left}}th{{background:#222;color:white}}.state-submitted{{color:#087f23;font-weight:700}}.state-needs_human,.state-needs_input{{color:#a15c00;font-weight:700}}.state-failed{{color:#b00020;font-weight:700}}</style></head>
<body><h1>Job Beast</h1><p>{len(rows)} tracked applications</p>
<table><thead><tr><th>Company</th><th>Role</th><th>Score</th><th>State</th><th>Attention</th><th>Link</th></tr></thead>
<tbody>{''.join(body) or '<tr><td colspan="6">No applications yet</td></tr>'}</tbody></table></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
