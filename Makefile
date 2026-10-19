.PHONY: install test doctor init fetch rank ai-check ai-watch ai-models operate
install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

test:
	PYTHONPATH=src python3 -m pytest -q

doctor:
	PYTHONPATH=src python3 -m jobbeast doctor

init:
	PYTHONPATH=src python3 -m jobbeast init

fetch:
	PYTHONPATH=src python3 -m jobbeast fetch-simplify

rank:
	PYTHONPATH=src python3 -m jobbeast rank --top 25

ai-check:
	omniroute providers test-all && omniroute chat --no-history -m jobbeast "Reply with exactly: ok"

ai-watch:
	omniroute usage logs --follow --limit 20

ai-models:
	python3 scripts/models_used.py --since "$$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)"

operate:
	scripts/operate.sh $(JOB)
