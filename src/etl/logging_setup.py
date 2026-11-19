"""Structured, human-readable logging setup (standard library only).

Two handlers are attached to the shared ``etl`` logger:

* a console handler with a compact ``HH:MM:SS | LEVEL | stage | message``
  format, and
* a per-run file handler in ``logs/`` (``logs/etl_<run_id>.log``) that also
  records the ``run_id`` and uses full timestamps.

Stage modules log through ``logging.getLogger(__name__)`` (i.e. ``etl.extract``
etc.) with key=value message payloads so the output stays greppable while
remaining pleasant to read.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "etl"

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s [run_id=%(run_id)s]"
_CONSOLE_DATEFMT = "%H:%M:%S"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _RunIdFilter(logging.Filter):
    """Stamp every record with the current run id (used by the file format)."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id  # type: ignore[attr-defined]
        return True


def setup_logging(run_id: str, logs_dir: Path, level: str = "INFO") -> Path:
    """Configure console + per-run file logging and return the log file path.

    Calling this more than once replaces the previous handlers, so repeated
    in-process runs (e.g. inside the test suite) do not duplicate output.

    Args:
        run_id: Identifier of the current run; stamped on every file record.
        logs_dir: Directory that holds per-run log files (created if missing).
        level: Logging level name (DEBUG, INFO, WARNING, ERROR).

    Returns:
        The path of the log file written for this run.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(numeric_level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"etl_{run_id}.log"

    console = logging.StreamHandler()
    console.setLevel(numeric_level)
    console.setFormatter(
        logging.Formatter(_CONSOLE_FORMAT, datefmt=_CONSOLE_DATEFMT)
    )
    console.addFilter(_RunIdFilter(run_id))

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATEFMT)
    )
    file_handler.addFilter(_RunIdFilter(run_id))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.debug("logging.initialised console=on file=%s level=%s", log_path, level)
    return log_path
