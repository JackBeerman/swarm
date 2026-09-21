.PHONY: setup test lint check shadow backfill analyze sweep score calibrate probe fastlane fastlane-score paper clean

VENV := .venv

# Windows venvs put the interpreter in Scripts/, POSIX in bin/.
ifeq ($(OS),Windows_NT)
PY := $(VENV)/Scripts/python.exe
else
PY := $(VENV)/bin/python
endif

setup:                 ## create venv, install deps, create .env
	python -m venv $(VENV)
	$(PY) -m pip install --upgrade pip -q
	$(PY) -m pip install -r requirements.txt -q
	@test -f .env || (cp .env.example .env && echo "created .env -- add your TYPESAFE_API_KEY")
	@echo "done. next: make test"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

check: lint test

shadow:                ## collect triage verdicts (safe, no orders)
	$(PY) shadow.py --collect --limit 200

backfill:              ## fill resolved outcomes (settlement lands in minutes)
	$(PY) shadow.py --backfill

score:
	$(PY) shadow.py --score

calibrate:             ## price vs frequency, counted by event
	$(PY) shadow.py --calibrate

probe:                 ## re-check the restricted veto against live Jev (~$0.001)
	$(PY) tools/probe_restricted.py

analyze:
	$(PY) shadow.py --analyze

sweep:
	$(PY) shadow.py --sweep gate_score

paper:                 ## full pipeline, logs orders instead of placing them
	SWARM_MODE=paper $(PY) daemon.py

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ .mypy_cache

# Deliberately no `make live` target. Live trading should be a conscious
# command typed in a terminal, not a target you can fat-finger from a
# tab-complete.

fastlane:              ## shadow-only: Jev reads headlines, prices are followed (no orders)
	$(PY) fastlane.py --tags nfl --start-window 6 --minutes 240

fastlane-score:
	$(PY) fastlane.py --score
