"""iot-etl-pipeline: a small, engineering-minded IoT telemetry ETL pipeline.

Stages live in dedicated modules:

* :mod:`etl.extract`      -- load raw CSVs, checksum them, count rows
* :mod:`etl.validate`     -- declarative rule catalogue / rule engine
* :mod:`etl.transform`    -- dedupe, quarantine, coerce, derive fields
* :mod:`etl.load`         -- idempotent upsert into a SQLite warehouse
* :mod:`etl.report`       -- data-quality report (JSON + Markdown)
* :mod:`etl.orchestrator` -- wires the stages together
* :mod:`etl.cli`          -- argparse command line interface

The repository root also contains a tiny ``etl/`` shim package so that
``python -m etl ...`` works from a fresh clone without ``pip install``;
it simply redirects ``etl.__path__`` to this ``src/etl`` directory.
"""

__version__ = "1.0.0"
