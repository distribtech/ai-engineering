# Усе запускається з цієї папки. Потрібні Python 3.11+ і Ollama (див. README).
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: run test web metrics install

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

run:            ## тести + прогін усієї матриці з experiments.json + REPORT.md
	$(PY) main.py

metrics:        ## лише перерахувати метрики з results/runs.jsonl
	$(PY) main.py --metrics-only --skip-tests

test:
	$(PY) main.py --metrics-only --no-report

web:
	$(PY) app/server.py
