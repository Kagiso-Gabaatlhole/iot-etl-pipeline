"""Shared fixtures for the test suite.

The tests never touch the committed ``data/raw`` files: every test gets a
small synthetic dataset (420 rows, seed 7) generated into ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytest

from etl.extract import READINGS_COLUMNS
from etl.validate import Rule, load_rules
from pipeline.generate_data import InjectionPlan, build_devices, generate_dataset

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Deterministic injection plan used by the small test dataset
#: (400 clean + 20 duplicates = 420 rows; 36 mutated rows).
TEST_PLAN = InjectionPlan(
    duplicates=20,
    missing_values=8,
    out_of_range=8,
    future_timestamps=6,
    malformed=8,
    unknown_devices=6,
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path of the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def rule_catalogue() -> list[Rule]:
    """The real rule catalogue from ``config/validation_rules.json``."""
    return load_rules(REPO_ROOT / "config" / "validation_rules.json")


@pytest.fixture()
def make_row() -> Callable[..., dict[str, str]]:
    """Factory for raw telemetry row dicts with overridable defaults."""

    def _make(**overrides: str) -> dict[str, str]:
        row = {
            "reading_id": "R0000001",
            "device_id": "DEV001",
            "timestamp": "2026-09-15T10:00:00",
            "temperature_c": "20.0",
            "humidity_pct": "50.0",
            "power_kw": "1.5",
        }
        row.update(overrides)
        return row

    return _make


@pytest.fixture()
def registry() -> pd.DataFrame:
    """Device registry frame built from the generator's device catalogue."""
    return pd.DataFrame(build_devices())


@pytest.fixture()
def known_device_ids() -> list[str]:
    """Device ids accepted by the ``VR006`` in_set predicate."""
    return [device["device_id"] for device in build_devices()]


@pytest.fixture()
def dataset(tmp_path: Path) -> dict[str, Any]:
    """Small raw dataset with every injected quality problem (seed 7)."""
    summary = generate_dataset(
        tmp_path / "raw",
        seed=7,
        base_rows=400,
        plan=TEST_PLAN,
    )
    summary["plan"] = TEST_PLAN
    return summary


@pytest.fixture()
def clean_dataset(tmp_path: Path) -> dict[str, Any]:
    """Small raw dataset without any injected problems (validate passes)."""
    plan = InjectionPlan(
        duplicates=0,
        missing_values=0,
        out_of_range=0,
        future_timestamps=0,
        malformed=0,
        unknown_devices=0,
    )
    return generate_dataset(tmp_path / "raw_clean", seed=11, base_rows=200, plan=plan)


@pytest.fixture()
def make_config(tmp_path: Path, dataset: dict[str, Any]) -> Callable[..., Path]:
    """Factory writing a pipeline config JSON that points into ``tmp_path``."""

    def _make(*, abort_on_error: bool = False) -> Path:
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        config = {
            "pipeline_name": "pytest-pipeline",
            "version": "9.9.9",
            "paths": {
                "readings_csv": str(dataset["readings"]["path"]),
                "device_registry_csv": str(dataset["device_registry"]["path"]),
                "validation_rules": str(REPO_ROOT / "config" / "validation_rules.json"),
                "warehouse_db": str(workspace / "warehouse.db"),
                "results_dir": str(workspace / "results"),
                "logs_dir": str(workspace / "logs"),
                "quarantine_dir": str(workspace / "quarantine"),
            },
            "validation": {"abort_on_error": abort_on_error},
            "logging": {"level": "INFO"},
        }
        path = workspace / f"config_{str(abort_on_error).lower()}.json"
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    return _make


def frame_from_rows(rows: list[dict[str, str]]) -> pd.DataFrame:
    """Build a raw frame with the canonical column order."""
    return pd.DataFrame(rows, columns=list(READINGS_COLUMNS))
