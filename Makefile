# IoT telemetry ETL pipeline — build / test / run / report targets.
# Requires GNU make. On Windows without make, use the documented
# `py -3.12 ...` equivalents (see README "Quickstart").

PYTHON ?= python

.PHONY: build validate run report test

## Regenerate the synthetic raw CSVs in data/raw/ (seeded, deterministic).
build:
	$(PYTHON) src/pipeline/generate_data.py

## Extract + validate only; writes rejects CSV and the validation summary.
validate:
	$(PYTHON) -m etl validate --config config/pipeline_config.json

## Full pipeline: extract -> validate -> transform -> load -> report.
run:
	$(PYTHON) -m etl run --config config/pipeline_config.json

## Rebuild results/quality_report.{json,md} from the latest stored run artifacts.
report:
	$(PYTHON) -m etl report --config config/pipeline_config.json

## Run the pytest suite.
test:
	$(PYTHON) -m pytest tests -q
