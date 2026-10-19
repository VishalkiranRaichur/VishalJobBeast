#!/usr/bin/env bash
# Run the Claude browser operator for one job on Codex/Gemini instead of
# Claude credits, by launching Claude Code through OmniRoute.
#
#   scripts/operate.sh            # next eligible job
#   scripts/operate.sh <job_id>   # a specific job
#
# OPERATOR_MODEL  OmniRoute combo to use (default: jobbeast-operator, strong models only)
# SUBMIT=0        prepare and fill but do not authorize submission
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
MODEL="${OPERATOR_MODEL:-jobbeast-operator}"
OMNI_URL="http://localhost:20128"

if ! curl -s -o /dev/null -m 3 "$OMNI_URL/"; then
  echo "OmniRoute is not running; starting it..."
  omniroute serve --daemon >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do curl -s -o /dev/null -m 2 "$OMNI_URL/" && break; sleep 1; done
  curl -s -o /dev/null -m 3 "$OMNI_URL/" || { echo "OmniRoute did not start. Run: omniroute serve --daemon"; exit 1; }
fi

JOB="${1:-}"
if [[ -z "$JOB" ]]; then
  JOB="$("$PY" -m jobbeast next | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' 2>/dev/null || true)"
  [[ -n "$JOB" ]] || { echo "No eligible job available right now."; exit 1; }
fi

SUBMIT_FLAG=(--authorize-submission)
[[ "${SUBMIT:-1}" == "0" ]] && SUBMIT_FLAG=()
"$PY" -m jobbeast handoff "$JOB" ${SUBMIT_FLAG[@]+"${SUBMIT_FLAG[@]}"} | tail -n 6
OUT="$ROOT/output/$JOB"
[[ -f "$OUT/claude_browser_prompt.md" ]] || { echo "Handoff did not produce $OUT/claude_browser_prompt.md"; exit 1; }

START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat <<EOF

=== Operator running on OmniRoute combo: $MODEL ===
$(omniroute combo list 2>/dev/null | grep -F " $MODEL " || true)
Watch the model answering each request live in another terminal:  make ai-watch
Claude credits are not used. For a Claude-backed run instead, just run: claude
EOF

omniroute run claude --model "$MODEL" -- --chrome \
  "You are the Job Beast browser operator. Follow $OUT/claude_browser_prompt.md exactly, step by step. Job context: $OUT/application_context.md. Write the outcome to $OUT/claude_result.json." || true

echo
echo "=== Models used for job $JOB (since $START) ==="
"$PY" "$ROOT/scripts/models_used.py" --since "$START" --combo "$MODEL" --out "$OUT/models_used.json"
"$PY" -m jobbeast import-result "$JOB" || echo "import-result did not record a final outcome; check $OUT/claude_result.json"
