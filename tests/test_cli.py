"""CLI tests: command exit codes and failure paths.

Exit-code contract under test:

* 0 -- success (``run`` on the fixture dataset, ``report`` after a run),
* 1 -- validation failed at error severity (``validate``; ``run`` with
  ``abort_on_error=true``),
* 2 -- bad usage/config (missing file, invalid JSON, unknown run id) and the
  ``python -m etl`` smoke tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from etl.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clean_config_payload(clean_dataset: dict, workspace: Path) -> dict:
    return {
        "pipeline_name": "pytest-clean",
        "version": "9.9.9",
        "paths": {
            "readings_csv": str(clean_dataset["readings"]["path"]),
            "device_registry_csv": str(clean_dataset["device_registry"]["path"]),
            "validation_rules": str(REPO_ROOT / "config" / "validation_rules.json"),
            "warehouse_db": str(workspace / "warehouse.db"),
            "results_dir": str(workspace / "results"),
            "logs_dir": str(workspace / "logs"),
            "quarantine_dir": str(workspace / "quarantine"),
        },
        "validation": {"abort_on_error": False},
        "logging": {"level": "INFO"},
    }


def test_run_succeeds_and_writes_all_artifacts(make_config, tmp_path: Path) -> None:
    config_path = make_config()
    exit_code = main(["run", "--config", str(config_path), "--run-id", "cli-run-ok"])

    assert exit_code == 0
    workspace = tmp_path / "ws"
    for artifact in (
        "results/quality_report.json",
        "results/quality_report.md",
        "results/rejects_cli-run-ok.csv",
        "results/validation_summary_cli-run-ok.json",
        "quarantine/quarantine_cli-run-ok.csv",
        "logs/etl_cli-run-ok.log",
        "warehouse.db",
    ):
        assert (workspace / artifact).exists(), f"missing artifact: {artifact}"

    report = json.loads((workspace / "results/quality_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert report["validation"]["rejected_rows"] == 36
    assert report["warehouse"]["table_rows"] == 364


def test_validate_fails_with_exit_code_1_when_errors_present(make_config, tmp_path: Path) -> None:
    config_path = make_config()
    exit_code = main(["validate", "--config", str(config_path), "--run-id", "cli-validate-fail"])

    assert exit_code == 1
    workspace = tmp_path / "ws"
    summary = json.loads(
        (workspace / "results/validation_summary_cli-validate-fail.json").read_text(encoding="utf-8")
    )
    assert summary["validation"]["status"] == "fail"
    assert summary["validation"]["rejected_rows"] == 36
    assert (workspace / "results/rejects_cli-validate-fail.csv").is_file()


def test_validate_passes_with_exit_code_0_on_clean_data(
    clean_dataset: dict, tmp_path: Path
) -> None:
    workspace = tmp_path / "clean-ws"
    config_path = workspace / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(_clean_config_payload(clean_dataset, workspace)), encoding="utf-8")

    exit_code = main(["validate", "--config", str(config_path), "--run-id", "cli-validate-ok"])

    assert exit_code == 0
    summary = json.loads(
        (workspace / "results/validation_summary_cli-validate-ok.json").read_text(encoding="utf-8")
    )
    assert summary["validation"]["status"] == "pass"
    assert summary["validation"]["rejected_rows"] == 0


def test_run_aborts_with_exit_code_1_when_abort_on_error(make_config, tmp_path: Path) -> None:
    config_path = make_config(abort_on_error=True)
    exit_code = main(["run", "--config", str(config_path), "--run-id", "cli-abort"])

    assert exit_code == 1
    workspace = tmp_path / "ws"
    report = json.loads((workspace / "results/quality_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "aborted_after_validation"
    assert report["warehouse"]["executed"] is False
    assert not (workspace / "warehouse.db").exists(), "load must not run after an abort"
    assert (workspace / "results/rejects_cli-abort.csv").is_file(), "report artifacts still written"


def test_report_command_after_run(make_config) -> None:
    config_path = make_config()
    assert main(["run", "--config", str(config_path), "--run-id", "cli-report-src"]) == 0
    assert main(["report", "--config", str(config_path)]) == 0


def test_report_command_unknown_run_id_exits_2(make_config) -> None:
    config_path = make_config()
    assert main(["report", "--config", str(config_path), "--run-id", "no-such-run"]) == 2


def test_missing_config_file_exits_2(tmp_path: Path) -> None:
    assert main(["run", "--config", str(tmp_path / "missing.json")]) == 2


def test_invalid_json_config_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ nope", encoding="utf-8")
    assert main(["run", "--config", str(bad)]) == 2


def test_config_missing_key_exits_2(tmp_path: Path) -> None:
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"pipeline_name": "x", "paths": {"readings_csv": "a"}}), encoding="utf-8")
    assert main(["run", "--config", str(partial)]) == 2


def test_invalid_run_id_exits_2(make_config) -> None:
    config_path = make_config()
    assert main(["run", "--config", str(config_path), "--run-id", "bad/run/id"]) == 2


def test_missing_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == 2


def test_python_dash_m_entrypoint_runs_end_to_end(make_config) -> None:
    """`python -m etl` must work from the repository root (zero-install shim)."""
    config_path = make_config()
    completed = subprocess.run(
        [sys.executable, "-m", "etl", "run", "--config", str(config_path), "--run-id", "cli-subprocess"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    assert "run.finish" in completed.stdout + completed.stderr


def test_python_dash_m_entrypoint_bad_config_exits_2(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "etl", "run", "--config", str(tmp_path / "missing.json")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2
    assert "config file not found" in completed.stderr
