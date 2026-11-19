"""Tests for the extract stage: checksums, row counts and file handling."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from etl.extract import extract, sha256_of


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    payload = b"iot-telemetry\n" * 100
    target = tmp_path / "sample.bin"
    target.write_bytes(payload)
    assert sha256_of(target) == hashlib.sha256(payload).hexdigest()


def test_extract_returns_rows_and_checksums(dataset: dict) -> None:
    result = extract(
        Path(dataset["readings"]["path"]),
        Path(dataset["device_registry"]["path"]),
    )
    assert result.rows_read == 420
    assert len(result.readings) == 420
    assert len(result.device_registry) == 25
    assert result.readings_meta.sha256 == dataset["readings"]["sha256"]
    assert len(result.readings_meta.sha256) == 64
    assert result.readings_meta.row_count == len(result.readings)
    assert len(result.files) == 2


def test_extract_preserves_raw_strings(dataset: dict) -> None:
    result = extract(
        Path(dataset["readings"]["path"]),
        Path(dataset["device_registry"]["path"]),
    )
    sample = result.readings.iloc[0]
    assert all(isinstance(value, str) for value in sample.to_dict().values())


def test_extract_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract(tmp_path / "nope.csv", tmp_path / "also-nope.csv")


def test_extract_missing_column_raises(tmp_path: Path) -> None:
    readings = tmp_path / "readings.csv"
    readings.write_text("reading_id,device_id\nR1,DEV001\n", encoding="utf-8")
    registry = tmp_path / "registry.csv"
    registry.write_text("device_id,device_type,site,firmware\nDEV001,t,a,v1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing expected columns"):
        extract(readings, registry)
