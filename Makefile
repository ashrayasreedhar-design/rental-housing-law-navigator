# Rental Housing Law Navigator -- one-command reproduction.
#
# Everything a judge needs:  make reproduce
#
# Steps that need network (fetch, geocode) run on Replit. Steps that need API
# keys (extract, signals) read them from environment / Replit Secrets -- never
# the repo.

PY := python3
SRC := PYTHONPATH=src $(PY) -m
AS_OF ?= 2026-10-01

.PHONY: help setup fetch probe clean-data geocode signals extract validate lookups changes serve reproduce clean audit

help:
	@echo "Rental Housing Law Navigator"
	@echo ""
	@echo "  make setup      install dependencies"
	@echo "  make probe      dry-run the fetcher: robots + config check, no requests"
	@echo "  make fetch      retrieve allowlisted authority + signal sources   [network]"
	@echo "  make clean-data clean + quality-flag the 500 addresses"
	@echo "  make geocode    resolve 500 addresses to jurisdictions, cached    [network]"
	@echo "  make signals    extract T1-T5 change signals from all documents   [API keys]"
	@echo "  make extract    dual-model extraction (Claude + OpenAI)           [API keys]"
	@echo "  make validate   schema + verbatim-quote + coverage checks"
	@echo "  make lookups    Module B: per-address applicable rules"
	@echo "  make changes    Module C: change tests T1-T5"
	@echo "  make audit      data-quality red flags + compliance report"
	@echo "  make serve      FastAPI backend for the frontend"
	@echo "  make reproduce  everything above in order"
	@echo ""
	@echo "  AS_OF=$(AS_OF)  (override: make lookups AS_OF=2027-07-02)"

setup:
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

probe:
	$(SRC) rhln.fetch --dry-run

fetch:
	$(SRC) rhln.fetch --tier both

clean-data:
	$(SRC) rhln.clean

geocode: clean-data
	$(SRC) rhln.geocode

signals:
	$(SRC) rhln.signals

extract:
	$(SRC) rhln.extract --out out/rules_raw.json

validate:
	$(SRC) rhln.validate --rules out/rules_raw.json --out submission/rules.json

lookups:
	$(SRC) rhln.lookup --as-of $(AS_OF) --out submission/lookups.json

changes:
	$(SRC) rhln.changes --tests dev/change_tests.json --out submission/changes.json

audit:
	$(SRC) rhln.audit --out out/audit_report.json

serve:
	uvicorn rhln.api:app --host 0.0.0.0 --port 8000 --app-dir src

reproduce: setup fetch clean-data geocode signals extract validate lookups changes audit
	@echo ""
	@echo "=== Reproduction complete ==="
	@ls -la submission/ out/

clean:
	rm -rf out/*.json submission/*.json corpus/text_team/*.txt
