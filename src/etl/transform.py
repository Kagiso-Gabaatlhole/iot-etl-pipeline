"""Transform stage: dedupe, quarantine, coerce and derive.

Order of operations (each step is a pure function of the raw frame plus the
validation outcome):

1. **Drop exact duplicates** -- rows identical across all raw columns keep
   their first occurrence (the ``unique`` rule reports them as warnings).
2. **Quarantine error-level rows** -- rows with at least one ``error``
   violation are removed from the load path (the caller writes them to
   ``data/quarantine/``).
3. **Type coercion** -- measurement columns become floats, ``hour`` becomes an
   integer. Rows reaching this point already passed the ``numeric`` rules, so
   coercion failures surface as errors (exit code 2) rather than silent NaNs.
4. **Derive fields** -- ``hour`` and ``date`` from the (validated) ISO
   timestamp, ``device_type`` from the device registry, plus a
   ``warning_flags`` column joining any warn-severity rule ids with ``|``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from etl.validate import ValidationOutcome

logger = logging.getLogger(__name__)

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"

#: Columns added by the transform stage.
DERIVED_COLUMNS: tuple[str, ...] = ("hour", "device_type", "date", "warning_flags")

#: Measurement columns that must become floats.
FLOAT_COLUMNS: tuple[str, ...] = ("temperature_c", "humidity_pct", "power_kw")


@dataclass(frozen=True)
class TransformResult:
    """Output of the transform stage.

    Attributes:
        frame: Clean, typed frame ready for the warehouse.
        rows_in: Raw rows entering the stage.
        duplicates_dropped: Exact-duplicate rows removed.
        quarantined_rows: Error-level rows removed.
        rows_out: Clean rows leaving the stage.
        flagged_rows: Surviving rows carrying at least one warning.
    """

    frame: pd.DataFrame
    rows_in: int
    duplicates_dropped: int
    quarantined_rows: int
    rows_out: int
    flagged_rows: int

    def to_dict(self) -> dict[str, int]:
        """JSON-serialisable stage statistics."""
        return {
            "rows_in": self.rows_in,
            "duplicates_dropped": self.duplicates_dropped,
            "quarantined_rows": self.quarantined_rows,
            "rows_out": self.rows_out,
            "flagged_rows": self.flagged_rows,
        }


def transform(
    frame: pd.DataFrame,
    outcome: ValidationOutcome,
    device_registry: pd.DataFrame,
) -> TransformResult:
    """Apply dedupe, quarantine, coercion and derivations.

    Args:
        frame: Raw string-typed telemetry frame.
        outcome: Result of the validate stage (provides the row flags).
        device_registry: Registry frame with ``device_id``/``device_type``.

    Returns:
        :class:`TransformResult` with the clean frame and stage counters.

    Raises:
        ValueError: A row that passed validation still fails numeric/timestamp
            coercion (indicates a rule-catalogue gap; CLI exits with code 2).
    """
    work = frame.copy()
    rows_in = len(work)

    work = work.drop_duplicates(keep="first")
    duplicates_dropped = rows_in - len(work)

    rejected = list(outcome.error_flags)
    before_quarantine = len(work)
    work = work[~work.index.isin(rejected)].copy()
    quarantined_rows = before_quarantine - len(work)

    for column in FLOAT_COLUMNS:
        work[column] = pd.to_numeric(work[column], errors="raise")

    timestamps = pd.to_datetime(work["timestamp"], format=_ISO_FORMAT, errors="raise")
    work["hour"] = timestamps.dt.hour.astype("int64")
    work["date"] = timestamps.dt.strftime("%Y-%m-%d")

    type_by_device: dict[str, str] = {}
    if {"device_id", "device_type"}.issubset(device_registry.columns):
        type_by_device = dict(
            zip(device_registry["device_id"].astype(str), device_registry["device_type"].astype(str))
        )
    work["device_type"] = (
        work["device_id"].map(type_by_device).fillna("unknown").astype(str)
    )

    work["warning_flags"] = [
        "|".join(outcome.warn_flags.get(int(index), [])) for index in work.index
    ]
    work = work.reset_index(drop=True)

    flagged_rows = int((work["warning_flags"] != "").sum())
    result = TransformResult(
        frame=work,
        rows_in=rows_in,
        duplicates_dropped=duplicates_dropped,
        quarantined_rows=quarantined_rows,
        rows_out=len(work),
        flagged_rows=flagged_rows,
    )
    logger.info(
        "transform.done rows_in=%d duplicates_dropped=%d quarantined=%d rows_out=%d flagged=%d",
        result.rows_in, result.duplicates_dropped, result.quarantined_rows,
        result.rows_out, result.flagged_rows,
    )
    return result


def quarantine_frame(frame: pd.DataFrame, outcome: ValidationOutcome) -> pd.DataFrame:
    """Return the raw error-level rows that must be quarantined.

    Args:
        frame: The raw frame that was validated.
        outcome: Result of the validate stage.

    Returns:
        Copy of the rejected rows with ``source_row`` and ``error_rules``
        columns prepended/appended.
    """
    indices = sorted(outcome.error_flags)
    quarantined = frame.loc[indices].copy()
    quarantined.insert(0, "source_row", indices)
    quarantined["error_rules"] = [";".join(outcome.error_flags[i]) for i in indices]
    return quarantined


def write_quarantine(frame: pd.DataFrame, outcome: ValidationOutcome, path: Path) -> int:
    """Write quarantined rows to ``path`` and return the row count.

    Args:
        frame: The raw frame that was validated.
        outcome: Result of the validate stage.
        path: Destination CSV path (parent directories are created).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    quarantined = quarantine_frame(frame, outcome)
    quarantined.to_csv(path, index=False)
    logger.info("transform.quarantine_written rows=%d path=%s", len(quarantined), path)
    return len(quarantined)
