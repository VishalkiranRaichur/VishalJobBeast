"""Summarize which OmniRoute models handled requests since a start time.

Reads OmniRoute's own call logs (~/.omniroute/call_logs/<date>/*.json), so it
works for any client routed through OmniRoute, including Claude Code.

    python scripts/models_used.py --since 2026-10-15T18:00:00Z [--combo jobbeast-operator] [--out path.json]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LOG_ROOT = Path.home() / ".omniroute" / "call_logs"


def collect(since: str, combo: str | None) -> list[dict]:
    rows = []
    for path in sorted(LOG_ROOT.glob("*/*.json")):
        if path.parent.name < since[:10]:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8")).get("summary") or {}
        except (OSError, ValueError):
            continue
        if summary.get("timestamp", "") < since:
            continue
        if combo and summary.get("comboName") != combo:
            continue
        rows.append(summary)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO-8601 UTC timestamp, e.g. 2026-10-15T18:00:00Z")
    ap.add_argument("--combo", help="Only count requests routed through this combo")
    ap.add_argument("--out", help="Also write the summary as JSON here")
    args = ap.parse_args()

    rows = collect(args.since, args.combo)
    ok = Counter(f"{r.get('provider')}/{r.get('model')}" for r in rows if r.get("status") == 200)
    failed = Counter(f"{r.get('provider')}/{r.get('model')} -> {r.get('status')}" for r in rows if r.get("status") != 200)

    if not rows:
        print("No OmniRoute requests recorded for this run.")
    else:
        print(f"Models that answered ({sum(ok.values())} requests):")
        for name, n in ok.most_common():
            print(f"  {n:4d}  {name}")
        if failed:
            print("Skipped or failed (OmniRoute fell back past these):")
            for name, n in failed.most_common():
                print(f"  {n:4d}  {name}")

    if args.out:
        Path(args.out).write_text(json.dumps({"since": args.since, "combo": args.combo, "answered": dict(ok), "failed": dict(failed)}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
