#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$ROOT/src"
python -m jobbeast doctor
python -m jobbeast init
python -m jobbeast fetch-simplify
python -m jobbeast rank --top 25
