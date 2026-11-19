"""Load stage: idempotent upsert into a SQLite warehouse.

Idempotence approach
--------------------
The warehouse table ``readings`` is keyed by the natural key ``reading_id``.
Every load executes ``INSERT ... ON CONFLICT(reading_id) DO UPDATE``, so
loading the same cleaned data twice leaves the table byte-identical -- row
counts *and* content do not change. Deliberately, ``readings`` carries **no**
per-run columns (no ``loaded_at``/``run_id``); provenance lives in the
append-only ``load_manifest`` table instead, one row per run with the run id,
timestamps and rows in/out/rejects/warnings counters.

Tables
------
``readings``
    One row per cleaned sensor reading (primary key ``reading_id``).
``load_manifest``
    One row per pipeline run: ``run_id``, ``started_at``, ``finished_at``,
    ``rows_in``, ``rows_out``, ``rejects``, ``warnings``, ``flagged_rows``,
    ``duplicates_dropped``, ``status``, ``source_sha256``. Re-running the same
    ``run_id`` updates its manifest entry rather than duplicating it.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

#: Physical column order of the ``readings`` table.
READINGS_COLUMNS: tuple[str, ...] = (
    "reading_id",
    "device_id",
    "timestamp",
    "temperature_c",
    "humidity_pct",
    "power_kw",
    "hour",
    "device_type",
    "date",
    "warning_flags",
)

_CREATE_READINGS = """
CREATE TABLE IF NOT EXISTS readings (
    reading_id   TEXT PRIMARY KEY,
    device_id    TEXT    NOT NULL,
    timestamp    TEXT    NOT NULL,
    temperature_c REAL   NOT NULL,
    humidity_pct  REAL   NOT NULL,
    power_kw      REAL   NOT NULL,
    hour          INTEGER NOT NULL,
    device_type   TEXT    NOT NULL,
    date          TEXT    NOT NULL,
    warning_flags TEXT    NOT NULL DEFAULT ''
)
"""

_CREATE_MANIFEST = """
CREATE TABLE IF NOT EXISTS load_manifest (
    run_id             TEXT PRIMARY KEY,
    started_at         TEXT    NOT NULL,
    finished_at        TEXT    NOT NULL,
    rows_in            INTEGER NOT NULL,
    rows_out           INTEGER NOT NULL,
    rejects            INTEGER NOT NULL,
    quarantined_rows   INTEGER NOT NULL,
    warnings           INTEGER NOT NULL,
    flagged_rows       INTEGER NOT NULL,
    duplicates_dropped INTEGER NOT NULL,
    status             TEXT    NOT NULL,
    source_sha256      TEXT    NOT NULL
)
"""

_PLACEHOLDERS = ", ".join("?" for _ in READINGS_COLUMNS)
_UPSERT_READINGS = f"""
INSERT INTO readings ({", ".join(READINGS_COLUMNS)})
VALUES ({_PLACEHOLDERS})
ON CONFLICT(reading_id) DO UPDATE SET
    device_id     = excluded.device_id,
    timestamp     = excluded.timestamp,
    temperature_c = excluded.temperature_c,
    humidity_pct  = excluded.humidity_pct,
    power_kw      = excluded.power_kw,
    hour          = excluded.hour,
    device_type   = excluded.device_type,
    date          = excluded.date,
    warning_flags = excluded.warning_flags
"""

_MANIFEST_COLUMNS: tuple[str, ...] = (
    "run_id",
    "started_at",
    "finished_at",
    "rows_in",
    "rows_out",
    "rejects",
    "quarantined_rows",
    "warnings",
    "flagged_rows",
    "duplicates_dropped",
    "status",
    "source_sha256",
)
_MANIFEST_UPSERT = f"""
INSERT INTO load_manifest ({", ".join(_MANIFEST_COLUMNS)})
VALUES ({", ".join("?" for _ in _MANIFEST_COLUMNS)})
ON CONFLICT(run_id) DO UPDATE SET
    started_at         = excluded.started_at,
    finished_at        = excluded.finished_at,
    rows_in            = excluded.rows_in,
    rows_out           = excluded.rows_out,
    rejects            = excluded.rejects,
    quarantined_rows   = excluded.quarantined_rows,
    warnings           = excluded.warnings,
    flagged_rows       = excluded.flagged_rows,
    duplicates_dropped = excluded.duplicates_dropped,
    status             = excluded.status,
    source_sha256      = excluded.source_sha256
"""


@dataclass(frozen=True)
class LoadManifest:
    """Provenance record appended to ``load_manifest`` for this run.

    ``rows_out`` is intentionally absent: the load stage fills it with the
    warehouse row count measured *after* the upsert (idempotence evidence).
    """

    run_id: str
    started_at: str
    finished_at: str
    rows_in: int
    rejects: int
    quarantined_rows: int
    warnings: int
    flagged_rows: int
    duplicates_dropped: int
    status: str
    source_sha256: str


@dataclass(frozen=True)
class LoadStats:
    """Result of the load stage.

    Attributes:
        rows_in: Clean rows handed to the load stage.
        rows_written: Rows upserted (inserted or updated).
        warehouse_rows: Row count of ``readings`` after the load.
    """

    rows_in: int
    rows_written: int
    warehouse_rows: int


def _bindable_rows(frame: pd.DataFrame) -> list[tuple[Any, ...]]:
    """Convert the clean frame into plain-Python tuples for sqlite3."""
    missing = [column for column in READINGS_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"load frame is missing expected columns: {', '.join(missing)}")
    rows: list[tuple[Any, ...]] = []
    for values in frame.loc[:, list(READINGS_COLUMNS)].itertuples(index=False, name=None):
        reading_id, device_id, timestamp, temperature_c, humidity_pct, power_kw, hour, device_type, date, warning_flags = values
        rows.append(
            (
                str(reading_id),
                str(device_id),
                str(timestamp),
                float(temperature_c),
                float(humidity_pct),
                float(power_kw),
                int(hour),
                str(device_type),
                str(date),
                str(warning_flags),
            )
        )
    return rows


def load(frame: pd.DataFrame, manifest: LoadManifest, db_path: Path) -> LoadStats:
    """Upsert the clean frame into SQLite and append the run manifest.

    Running this twice with identical data leaves ``readings`` unchanged
    (the conflict is resolved by ``DO UPDATE`` with identical values).

    Args:
        frame: Clean frame produced by the transform stage.
        manifest: Run provenance record to persist in ``load_manifest``.
        db_path: SQLite database file (created together with its parents).

    Returns:
        :class:`LoadStats` including the warehouse row count after the load.

    Raises:
        ValueError: The frame does not carry the expected warehouse columns.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _bindable_rows(frame)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(_CREATE_READINGS)
        connection.execute(_CREATE_MANIFEST)
        connection.executemany(_UPSERT_READINGS, rows)
        warehouse_rows = int(connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0])
        connection.execute(
            _MANIFEST_UPSERT,
            (
                manifest.run_id,
                manifest.started_at,
                manifest.finished_at,
                manifest.rows_in,
                warehouse_rows,  # rows_out = table count measured after the load
                manifest.rejects,
                manifest.quarantined_rows,
                manifest.warnings,
                manifest.flagged_rows,
                manifest.duplicates_dropped,
                manifest.status,
                manifest.source_sha256,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    stats = LoadStats(rows_in=len(rows), rows_written=len(rows), warehouse_rows=warehouse_rows)
    logger.info(
        "load.done rows_in=%d warehouse_rows=%d db=%s",
        stats.rows_in, stats.warehouse_rows, db_path,
    )
    return stats


def count_rows(db_path: Path, table: str = "readings") -> int:
    """Return the row count of ``table`` (raises if the table is absent)."""
    connection = sqlite3.connect(Path(db_path))
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def content_digest(db_path: Path, table: str = "readings") -> str:
    """SHA-256 over the ordered rows of ``table``.

    Used as idempotence evidence: two loads of the same data must produce the
    same digest.
    """
    connection = sqlite3.connect(Path(db_path))
    try:
        cursor = connection.execute(f"SELECT * FROM {table} ORDER BY 1")
        digest = hashlib.sha256()
        for row in cursor:
            digest.update(repr(row).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        connection.close()


def fetch_manifest(db_path: Path, run_id: str) -> dict[str, Any] | None:
    """Return the manifest row for ``run_id`` as a dict, or ``None``."""
    return _fetch_manifest_where(db_path, "run_id = ?", (run_id,))


def latest_manifest(db_path: Path) -> dict[str, Any] | None:
    """Return the most recently started manifest row, or ``None``."""
    return _fetch_manifest_where(db_path, "1=1", (), order_by="started_at DESC")


def _fetch_manifest_where(
    db_path: Path,
    where: str,
    params: Sequence[Any],
    order_by: str = "rowid",
) -> dict[str, Any] | None:
    if not Path(db_path).is_file():
        return None
    connection = sqlite3.connect(Path(db_path))
    try:
        try:
            cursor = connection.execute(
                f"SELECT {', '.join(_MANIFEST_COLUMNS)} FROM load_manifest "
                f"WHERE {where} ORDER BY {order_by} LIMIT 1",
                tuple(params),
            )
        except sqlite3.OperationalError:
            return None  # manifest table not created yet
        row = cursor.fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return dict(zip(_MANIFEST_COLUMNS, row))


def all_manifests(db_path: Path) -> list[dict[str, Any]]:
    """Return every manifest row ordered by start time (oldest first)."""
    if not Path(db_path).is_file():
        return []
    connection = sqlite3.connect(Path(db_path))
    try:
        try:
            cursor = connection.execute(
                f"SELECT {', '.join(_MANIFEST_COLUMNS)} FROM load_manifest ORDER BY started_at"
            )
        except sqlite3.OperationalError:
            return []
        rows = cursor.fetchall()
    finally:
        connection.close()
    return [dict(zip(_MANIFEST_COLUMNS, row)) for row in rows]


def iter_rows(db_path: Path, table: str = "readings") -> Iterable[tuple[Any, ...]]:
    """Iterate over ``table`` rows ordered by primary key (test helper)."""
    connection = sqlite3.connect(Path(db_path))
    try:
        yield from connection.execute(f"SELECT * FROM {table} ORDER BY 1")
    finally:
        connection.close()
