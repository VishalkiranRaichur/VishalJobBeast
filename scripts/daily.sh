#!/usr/bin/env bash
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"

# Each source is isolated. A source can fail without losing previously ingested jobs.
python -m jobbeast fetch-simplify || echo "[warn] Simplify fetch failed; keeping existing database."
python -m jobbeast rank --top 40
