PYTHON := .venv/bin/python
PIP := .venv/bin/pip
PYTEST := .venv/bin/pytest

.PHONY: setup migrate prepare-history refresh-data train-model evaluate-model test run run-prod scheduler diagnose-runtime worker queue-worker-smoke compute-health

setup:
	python3 -m venv .venv
	$(PIP) install -r requirements-fedora-core.txt
	cp -n .env.example .env || true
	$(PYTHON) -m scripts.sip migrate

migrate:
	$(PYTHON) -m scripts.sip migrate

refresh-data:
	$(PYTHON) -m scripts.sip refresh

prepare-history:
	$(PYTHON) -m scripts.sip prepare-history --league all

train-model:
	$(PYTHON) -m scripts.sip train-model --league all

evaluate-model:
	$(PYTHON) -m scripts.sip evaluate

test:
	$(PYTEST) -q

run:
	SIP_HOST=127.0.0.1 $(PYTHON) -m scripts.sip run

run-prod:
	$(PYTHON) -m scripts.sip run

scheduler:
	$(PYTHON) -m scripts.sip scheduler

diagnose-runtime:
	$(PYTHON) -m sports.compute.runtime_diagnostics

worker:
	$(PYTHON) -m raghub_worker

queue-worker-smoke:
	$(PYTHON) -m scripts.compute queue-smoke

compute-health:
	$(PYTHON) -m scripts.compute health
