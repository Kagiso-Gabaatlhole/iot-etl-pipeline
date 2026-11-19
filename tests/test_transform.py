"""Tests for the transform stage: dedupe, quarantine, coercion and derivations."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

from etl.extract import READINGS_COLUMNS, extract
from etl.transform import transform, write_quarantine
from etl.validate import ValidationOutcome, load_rules, validate_frame


def _outcome(
    error_flags: dict[int, list[str]] | None = None,
    warn_flags: dict[int, list[str]] | None = None,
    rows_total: int = 0,
) -> ValidationOutcome:
    """Build a minimal ValidationOutcome for isolated transform tests."""
    return ValidationOutcome(
        rows_total=rows_total,
        rule_counts={},
        error_flags=error_flags or {},
        warn_flags=warn_flags or {},
        status="fail" if error_flags else "pass",
        evaluated_at="2026-10-01T00:00:00",
    )


def test_dedupe_drops_exact_duplicates(make_row: Callable[..., dict[str, str]]) -> None:
    first = make_row()
    second = make_row(reading_id="R0000002", temperature_c="21.5")
    frame = pd.DataFrame([first, dict(first), dict(first), second], columns=list(READINGS_COLUMNS))

    result = transform(frame, _outcome(rows_total=4), device_registry=pd.DataFrame())

    assert result.rows_in == 4
    assert result.duplicates_dropped == 2
    assert result.quarantined_rows == 0
    assert result.rows_out == 2
    assert list(result.frame["reading_id"]) == ["R0000001", "R0000002"]  # first kept


def test_error_rows_are_quarantined(make_row: Callable[..., dict[str, str]]) -> None:
    good = make_row()
    bad = make_row(reading_id="R0000002", power_kw="-3.0")
    frame = pd.DataFrame([good, bad], columns=list(READINGS_COLUMNS))

    result = transform(
        frame,
        _outcome(error_flags={1: ["VR012"]}, rows_total=2),
        device_registry=pd.DataFrame(),
    )

    assert result.quarantined_rows == 1
    assert result.rows_out == 1
    assert list(result.frame["reading_id"]) == ["R0000001"]


def test_derived_fields_hour_date_device_type(
    make_row: Callable[..., dict[str, str]],
    registry: pd.DataFrame,
) -> None:
    row = make_row(device_id="DEV002", timestamp="2026-09-15T23:30:00")
    frame = pd.DataFrame([row], columns=list(READINGS_COLUMNS))

    result = transform(frame, _outcome(rows_total=1), device_registry=registry)

    record = result.frame.iloc[0]
    assert int(record["hour"]) == 23
    assert record["date"] == "2026-09-15"
    assert record["device_type"] == "smart_plug"  # DEV002 -> second device type


def test_measurement_columns_are_coerced_to_floats(
    make_row: Callable[..., dict[str, str]],
) -> None:
    row = make_row(temperature_c="20.25", humidity_pct="55.5", power_kw="1.125")
    frame = pd.DataFrame([row], columns=list(READINGS_COLUMNS))

    result = transform(frame, _outcome(rows_total=1), device_registry=pd.DataFrame())

    record = result.frame.iloc[0]
    assert isinstance(record["temperature_c"], float)
    assert isinstance(record["humidity_pct"], float)
    assert isinstance(record["power_kw"], float)
    assert record["temperature_c"] == pytest.approx(20.25)
    assert str(result.frame["hour"].dtype).startswith("int")


def test_warning_flags_are_joined(
    make_row: Callable[..., dict[str, str]],
    registry: pd.DataFrame,
) -> None:
    flagged = make_row()
    clean = make_row(reading_id="R0000002")
    frame = pd.DataFrame([flagged, clean], columns=list(READINGS_COLUMNS))

    result = transform(
        frame,
        _outcome(warn_flags={0: ["VR013", "VR014"]}, rows_total=2),
        device_registry=registry,
    )

    assert result.frame.loc[0, "warning_flags"] == "VR013|VR014"
    assert result.frame.loc[1, "warning_flags"] == ""
    assert result.flagged_rows == 1


def test_unknown_device_type_falls_back(
    make_row: Callable[..., dict[str, str]],
    registry: pd.DataFrame,
) -> None:
    row = make_row(device_id="DEV042")
    frame = pd.DataFrame([row], columns=list(READINGS_COLUMNS))
    result = transform(frame, _outcome(rows_total=1), device_registry=registry)
    assert result.frame.loc[0, "device_type"] == "unknown"


def test_write_quarantine_csv(
    tmp_path: Path,
    make_row: Callable[..., dict[str, str]],
) -> None:
    good = make_row()
    bad = make_row(reading_id="R0000002", temperature_c="88.0")
    frame = pd.DataFrame([good, bad], columns=list(READINGS_COLUMNS))
    outcome = _outcome(error_flags={1: ["VR008", "VR013"]}, rows_total=2)

    target = tmp_path / "quarantine" / "quarantine_x.csv"
    written = write_quarantine(frame, outcome, target)

    assert written == 1
    quarantined = pd.read_csv(target, dtype=str, keep_default_na=False)
    assert len(quarantined) == 1
    assert quarantined.loc[0, "source_row"] == "1"
    assert quarantined.loc[0, "error_rules"] == "VR008;VR013"
    assert quarantined.loc[0, "temperature_c"] == "88.0"


def test_transform_counts_on_small_dataset(dataset: dict, rule_catalogue) -> None:
    """Integration check: the injected problems are removed exactly as planned."""
    extraction = extract(
        Path(dataset["readings"]["path"]),
        Path(dataset["device_registry"]["path"]),
    )
    outcome = validate_frame(
        extraction.readings,
        rule_catalogue,
        context={"known_device_ids": extraction.device_registry["device_id"].tolist()},
    )
    result = transform(extraction.readings, outcome, extraction.device_registry)

    assert result.rows_in == 420
    assert result.duplicates_dropped == dataset["plan"].duplicates  # 20
    assert result.quarantined_rows == dataset["plan"].mutations  # 36
    assert result.rows_out == 420 - 20 - 36  # 364
    assert set(result.frame["device_id"]) <= set(extraction.device_registry["device_id"])
    assert result.frame["reading_id"].is_unique
