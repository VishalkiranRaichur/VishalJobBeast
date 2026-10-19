#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"

# Safe scheduled run: refresh, rank, hydrate, tailor, and validate. It does not
# submit unless the caller explicitly sets AUTO_SUBMIT=1.
python -m jobbeast refresh
python -m jobbeast rank --top 100
if [[ "${AUTO_SUBMIT:-0}" == "1" ]]; then
  python -m jobbeast batch-apply --submit
else
  python -m jobbeast batch-prepare
fi
python -m jobbeast dashboard --output output/dashboard.html
