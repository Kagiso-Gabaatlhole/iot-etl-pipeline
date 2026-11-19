"""Argparse command line interface.

Commands::

    python -m etl run      --config config/pipeline_config.json [--run-id ID]
    python -m etl validate --config config/pipeline_config.json [--run-id ID]
    python -m etl report   --config config/pipeline_config.json [--run-id ID]

Exit codes:

=== ==========================================================================
Code Meaning
=== ==========================================================================
0    Command completed successfully.
1    Validation failed at error severity (``validate`` command, or ``run``
     with ``validation.abort_on_error=true``).
2    Bad usage/configuration or an unexpected error (missing files, invalid
     JSON, unknown run artifacts, unhandled exceptions).
=== ==========================================================================
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from secrets import token_hex
from typing import Sequence

from etl.config import ConfigError, load_config
from etl.logging_setup import setup_logging
from etl.orchestrator import PipelineResult, regenerate_report, run_pipeline, run_validation
from etl.report import ReportError
from etl.validate import RuleSpecError

logger = logging.getLogger(__name__)

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the ``etl`` command."""
    parser = argparse.ArgumentParser(
        prog="etl",
        description=(
            "Small IoT telemetry ETL pipeline: extract -> validate -> transform "
            "-> load -> report (synthetic data, SQLite warehouse)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    command_help = {
        "run": "full pipeline: extract, validate, transform, load, report",
        "validate": "extract + validate only (writes rejects CSV + summary)",
        "report": "rebuild quality_report.{json,md} from stored run artifacts",
    }
    for name, help_text in command_help.items():
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        subparser.add_argument(
            "--config",
            default="config/pipeline_config.json",
            help="path to the JSON pipeline configuration (default: %(default)s)",
        )
        subparser.add_argument(
            "--run-id",
            default=None,
            help="identifier for this run (default: generated from the timestamp)",
        )
        subparser.add_argument(
            "--log-level",
            default=None,
            choices=("DEBUG", "INFO", "WARNING", "ERROR"),
            help="override the configured console log level",
        )
    return parser


def default_run_id() -> str:
    """Generate a filename-safe run id such as ``run_20261029T101530_a1b2``."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"run_{stamp}_{token_hex(2)}"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: parse arguments, run the command, return the exit code.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success, 1 on fatal validation failure, 2 on configuration or
        unexpected errors. Argument parsing errors exit via argparse with 2.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id or default_run_id()
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        print(
            f"error: invalid --run-id {run_id!r}: use letters, digits, '.', '_' or '-'",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: run the CLI from the repository root or pass --config <path>", file=sys.stderr)
        return 2

    log_file = setup_logging(run_id, config.logs_dir, args.log_level or config.log_level)

    try:
        if args.command == "run":
            result: PipelineResult = run_pipeline(config, run_id)
        elif args.command == "validate":
            result = run_validation(config, run_id)
        else:
            result = regenerate_report(config, args.run_id)
    except (ConfigError, RuleSpecError, ReportError) as exc:
        logger.error("run.failed command=%s reason=%s exit_code=2", args.command, exc)
        return 2
    except Exception:
        logger.exception("run.unexpected_error command=%s exit_code=2", args.command)
        return 2

    logger.debug(
        "run.invocation command=%s run_id=%s exit_code=%d log_file=%s",
        args.command, result.run_id, result.exit_code, log_file,
    )
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
