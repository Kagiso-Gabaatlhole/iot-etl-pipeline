"""Tests for configuration loading (failures map to CLI exit code 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, payload: str) -> Path:
    path.write_text(payload, encoding="utf-8")
    return path


def _valid_payload(tmp_path: Path) -> dict:
    return {
        "pipeline_name": "cfg-test",
        "version": "1.2.3",
        "paths": {
            "readings_csv": str(tmp_path / "readings.csv"),
            "device_registry_csv": str(tmp_path / "registry.csv"),
            "validation_rules": str(tmp_path / "rules.json"),
            "warehouse_db": str(tmp_path / "warehouse.db"),
            "results_dir": str(tmp_path / "results"),
            "logs_dir": str(tmp_path / "logs"),
            "quarantine_dir": str(tmp_path / "quarantine"),
        },
        "validation": {"abort_on_error": True},
        "logging": {"level": "debug"},
    }


def test_load_valid_config(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.json", json.dumps(_valid_payload(tmp_path)))
    config = load_config(path)

    assert config.pipeline_name == "cfg-test"
    assert config.version == "1.2.3"
    assert config.abort_on_error is True
    assert config.log_level == "DEBUG"  # normalised to upper case
    assert config.readings_csv.is_absolute()
    assert config.results_dir == tmp_path / "results"


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.json")


def test_invalid_json_config(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.json", "{ this is not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)


def test_non_object_json_config(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.json", "[1, 2, 3]")
    with pytest.raises(ConfigError, match="JSON object"):
        load_config(path)


def test_missing_paths_object(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.json", json.dumps({"pipeline_name": "x"}))
    with pytest.raises(ConfigError, match="paths"):
        load_config(path)


def test_missing_single_path_key(tmp_path: Path) -> None:
    payload = _valid_payload(tmp_path)
    del payload["paths"]["warehouse_db"]
    path = _write(tmp_path / "config.json", json.dumps(payload))
    with pytest.raises(ConfigError, match="warehouse_db"):
        load_config(path)


def test_invalid_abort_on_error_type(tmp_path: Path) -> None:
    payload = _valid_payload(tmp_path)
    payload["validation"]["abort_on_error"] = "yes"
    path = _write(tmp_path / "config.json", json.dumps(payload))
    with pytest.raises(ConfigError, match="abort_on_error"):
        load_config(path)


def test_invalid_log_level(tmp_path: Path) -> None:
    payload = _valid_payload(tmp_path)
    payload["logging"]["level"] = "LOUD"
    path = _write(tmp_path / "config.json", json.dumps(payload))
    with pytest.raises(ConfigError, match="logging.level"):
        load_config(path)


def test_bom_tolerant_reading(tmp_path: Path) -> None:
    """Windows editors often prepend a UTF-8 BOM to JSON files."""
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_valid_payload(tmp_path)).encode("utf-8"))
    config = load_config(path)
    assert config.pipeline_name == "cfg-test"


def test_repository_config_is_structurally_valid() -> None:
    """The shipped config must parse and contain every required key."""
    raw = json.loads((REPO_ROOT / "config" / "pipeline_config.json").read_text(encoding="utf-8"))
    for key in (
        "readings_csv",
        "device_registry_csv",
        "validation_rules",
        "warehouse_db",
        "results_dir",
        "logs_dir",
        "quarantine_dir",
    ):
        assert key in raw["paths"]
    assert isinstance(raw["validation"]["abort_on_error"], bool)
    assert raw["logging"]["level"] in ("DEBUG", "INFO", "WARNING", "ERROR")
