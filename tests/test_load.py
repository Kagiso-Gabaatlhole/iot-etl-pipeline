"""Tests for the load stage: idempotent upserts and the run manifest."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from etl.load import (
    READINGS_COLUMNS,
    LoadManifest,
    all_manifests,
    content_digest,
    count_rows,
    fetch_manifest,
    load,
)


def make_frame(count: int = 3) -> pd.DataFrame:
    """Small warehouse-shaped frame (all READINGS_COLUMNS present)."""
    rows = [
        {
            "reading_id": f"T{index:05d}",
            "device_id": "DEV001",
            "timestamp": "2026-09-15T10:00:00",
            "temperature_c": 20.0 + index,
            "humidity_pct": 50.0,
            "power_kw": 1.0 + index,
            "hour": 10,
            "device_type": "thermostat",
            "date": "2026-09-15",
            "warning_flags": "",
        }
        for index in range(count)
    ]
    return pd.DataFrame(rows, columns=list(READINGS_COLUMNS))


def make_manifest(run_id: str, **overrides: object) -> LoadManifest:
    """LoadManifest with sensible defaults for tests."""
    values: dict[str, object] = {
        "run_id": run_id,
        "started_at": "2026-10-01T10:00:00",
        "finished_at": "2026-10-01T10:00:05",
        "rows_in": 3,
        "rejects": 1,
        "quarantined_rows": 1,
        "warnings": 2,
        "flagged_rows": 2,
        "duplicates_dropped": 0,
        "status": "success",
        "source_sha256": "abc123",
    }
    values.update(overrides)
    return LoadManifest(**values)  # type: ignore[arg-type]


def test_load_creates_tables_and_counts(tmp_path: Path) -> None:
    db = tmp_path / "warehouse.db"
    stats = load(make_frame(3), make_manifest("run-a"), db)

    assert stats.rows_in == 3
    assert stats.warehouse_rows == 3
    assert count_rows(db, "readings") == 3
    assert count_rows(db, "load_manifest") == 1


def test_load_is_idempotent(tmp_path: Path) -> None:
    """Loading the same cleaned data twice leaves identical table contents."""
    db = tmp_path / "warehouse.db"
    frame = make_frame(5)

    load(frame, make_manifest("run-1", rows_in=5), db)
    count_first = count_rows(db, "readings")
    digest_first = content_digest(db, "readings")

    load(frame, make_manifest("run-2", rows_in=5), db)
    count_second = count_rows(db, "readings")
    digest_second = content_digest(db, "readings")

    assert count_first == count_second == 5
    assert digest_first == digest_second
    # provenance is append-only: one manifest row per run
    manifests = all_manifests(db)
    assert [entry["run_id"] for entry in manifests] == ["run-1", "run-2"]
    assert manifests[0]["rows_out"] == manifests[1]["rows_out"] == 5


def test_load_updates_changed_values_via_upsert(tmp_path: Path) -> None:
    db = tmp_path / "warehouse.db"
    frame = make_frame(2)
    load(frame, make_manifest("run-1"), db)

    changed = frame.copy()
    changed.loc[0, "temperature_c"] = 99.5
    load(changed, make_manifest("run-2"), db)

    assert count_rows(db, "readings") == 2
    connection = sqlite3.connect(db)
    try:
        value = connection.execute(
            "SELECT temperature_c FROM readings WHERE reading_id = 'T00000'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert value == pytest.approx(99.5)


def test_manifest_upsert_keeps_one_row_per_run_id(tmp_path: Path) -> None:
    db = tmp_path / "warehouse.db"
    frame = make_frame(3)
    load(frame, make_manifest("same-run"), db)
    load(frame, make_manifest("same-run", rejects=9), db)

    manifests = all_manifests(db)
    assert len(manifests) == 1
    assert manifests[0]["rejects"] == 9


def test_manifest_fields_present(tmp_path: Path) -> None:
    db = tmp_path / "warehouse.db"
    load(make_frame(3), make_manifest("run-a"), db)

    manifest = fetch_manifest(db, "run-a")
    assert manifest is not None
    for key in (
        "run_id",
        "started_at",
        "finished_at",
        "rows_in",
        "rows_out",
        "rejects",
        "warnings",
        "flagged_rows",
        "duplicates_dropped",
        "status",
        "source_sha256",
    ):
        assert key in manifest, f"manifest missing {key}"
    assert manifest["rows_in"] == 3
    assert manifest["rows_out"] == 3  # table count measured after load
    assert manifest["rejects"] == 1


def test_fetch_missing_manifest_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "warehouse.db"
    load(make_frame(1), make_manifest("run-a"), db)
    assert fetch_manifest(db, "no-such-run") is None


def test_count_rows_missing_table_raises(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    with pytest.raises(sqlite3.OperationalError):
        count_rows(db, "readings")


def test_load_missing_column_raises(tmp_path: Path) -> None:
    broken = make_frame(1).drop(columns=["power_kw"])
    with pytest.raises(ValueError, match="missing expected columns"):
        load(broken, make_manifest("run-a"), tmp_path / "warehouse.db")
