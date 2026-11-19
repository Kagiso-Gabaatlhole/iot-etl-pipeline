"""Configuration loading and validation.

JSON is used on purpose (instead of YAML) so the pipeline only depends on the
standard library plus pandas. Relative paths inside a configuration file are
resolved against the current working directory, which means the CLI should be
invoked from the repository root.

Raised errors:

* :class:`ConfigError` -- the config file is missing, not valid JSON, or a
  required key is absent/wrongly typed. The CLI maps this to exit code 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Keys that must appear under ``"paths"`` in the pipeline configuration.
PATH_KEYS: tuple[str, ...] = (
    "readings_csv",
    "device_registry_csv",
    "validation_rules",
    "warehouse_db",
    "results_dir",
    "logs_dir",
    "quarantine_dir",
)

#: Accepted values for ``logging.level``.
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed or incomplete."""


@dataclass(frozen=True)
class PipelineConfig:
    """Validated pipeline configuration.

    Attributes:
        pipeline_name: Human-readable pipeline name (for reports/logs).
        version: Configuration/pipeline version string.
        config_path: Absolute path of the configuration file that was loaded.
        readings_csv: Raw telemetry CSV to extract.
        device_registry_csv: Device registry CSV (device_id -> device_type).
        validation_rules: JSON rule catalogue for the validate stage.
        warehouse_db: SQLite warehouse file written by the load stage.
        results_dir: Directory for rejects CSVs, summaries and reports.
        logs_dir: Directory for per-run log files.
        quarantine_dir: Directory for quarantined (error-level) rows.
        abort_on_error: When True, ``etl run`` stops before transform/load if
            validation finds any error-severity violation (exit code 1).
        log_level: Root logging level for the ``etl`` logger.
    """

    pipeline_name: str
    version: str
    config_path: Path
    readings_csv: Path
    device_registry_csv: Path
    validation_rules: Path
    warehouse_db: Path
    results_dir: Path
    logs_dir: Path
    quarantine_dir: Path
    abort_on_error: bool
    log_level: str

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the configuration (for reports)."""
        return {
            "pipeline_name": self.pipeline_name,
            "version": self.version,
            "config_path": str(self.config_path),
            "abort_on_error": self.abort_on_error,
            "log_level": self.log_level,
            "paths": {
                "readings_csv": str(self.readings_csv),
                "device_registry_csv": str(self.device_registry_csv),
                "validation_rules": str(self.validation_rules),
                "warehouse_db": str(self.warehouse_db),
                "results_dir": str(self.results_dir),
                "logs_dir": str(self.logs_dir),
                "quarantine_dir": str(self.quarantine_dir),
            },
        }


def _resolve(value: str, *, config_path: Path) -> Path:
    """Resolve ``value`` relative to the current working directory.

    Absolute paths are kept as-is; the config file location itself is only
    used for error messages (the CLI is documented to run from the repo root).
    """
    del config_path  # kept for signature clarity / future use
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def load_config(config_path: str | Path) -> PipelineConfig:
    """Load and validate a pipeline configuration file.

    Args:
        config_path: Path to a JSON configuration file.

    Returns:
        A fully validated :class:`PipelineConfig`.

    Raises:
        ConfigError: The file does not exist, is not valid JSON, is not a
            JSON object, or is missing/has invalid required keys.
    """
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        # utf-8-sig: tolerate a BOM, which Windows editors add by default.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {path} ({exc})") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config file must contain a JSON object: {path}")

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ConfigError("config is missing the required 'paths' object")
    missing = [key for key in PATH_KEYS if not isinstance(paths.get(key), str) or not paths.get(key)]
    if missing:
        raise ConfigError(f"config 'paths' is missing required string keys: {', '.join(missing)}")

    validation = raw.get("validation", {})
    if not isinstance(validation, dict):
        raise ConfigError("config key 'validation' must be an object")
    abort_on_error = validation.get("abort_on_error", False)
    if not isinstance(abort_on_error, bool):
        raise ConfigError("config key 'validation.abort_on_error' must be a boolean")

    logging_cfg = raw.get("logging", {})
    if not isinstance(logging_cfg, dict):
        raise ConfigError("config key 'logging' must be an object")
    log_level = str(logging_cfg.get("level", "INFO")).upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"config key 'logging.level' must be one of {', '.join(LOG_LEVELS)}")

    pipeline_name = raw.get("pipeline_name", "iot-telemetry-etl")
    version = raw.get("version", "0.0.0")
    if not isinstance(pipeline_name, str) or not isinstance(version, str):
        raise ConfigError("config keys 'pipeline_name' and 'version' must be strings")

    return PipelineConfig(
        pipeline_name=pipeline_name,
        version=version,
        config_path=path.resolve(),
        readings_csv=_resolve(paths["readings_csv"], config_path=path),
        device_registry_csv=_resolve(paths["device_registry_csv"], config_path=path),
        validation_rules=_resolve(paths["validation_rules"], config_path=path),
        warehouse_db=_resolve(paths["warehouse_db"], config_path=path),
        results_dir=_resolve(paths["results_dir"], config_path=path),
        logs_dir=_resolve(paths["logs_dir"], config_path=path),
        quarantine_dir=_resolve(paths["quarantine_dir"], config_path=path),
        abort_on_error=abort_on_error,
        log_level=log_level,
    )
