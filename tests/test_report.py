"""Tests for the report stage: required fields, rendering and a full run."""

from __future__ import annotations

import json
from pathlib import Path

from etl.config import load_config
from etl.orchestrator import run_pipeline
from etl.report import build_report, render_markdown, write_report

SUMMARY_STUB = {
    "schema_version": 1,
    "run_id": "run-stub",
    "started_at": "2026-10-01T10:00:00",
    "pipeline": {
        "name": "pytest-pipeline",
        "version": "9.9.9",
        "config_path": "/tmp/config.json",
    },
    "abort_on_error": False,
    "source_files": [
        {"path": "/data/iot_readings.csv", "sha256": "f" * 64, "row_count": 420}
    ],
    "validation": {
        "status": "fail",
        "rows_total": 420,
        "evaluated_at": "2026-10-01T10:00:02",
        "rules_evaluated": 15,
        "error_violations": 53,
        "warn_violations": 49,
        "rejected_rows": 36,
        "flagged_rows": 49,
        "rules": [
            {
                "id": "VR012",
                "description": "power_kw must be within 0..15 kW",
                "field": "power_kw",
                "severity": "error",
                "predicate": {"type": "range", "min": 0, "max": 15},
                "violations": 9,
            }
        ],
    },
}

MANIFEST_STUB = {
    "run_id": "run-stub",
    "started_at": "2026-10-01T10:00:00",
    "finished_at": "2026-10-01T10:00:09",
    "rows_in": 364,
    "rows_out": 364,
    "rejects": 36,
    "quarantined_rows": 36,
    "warnings": 49,
    "flagged_rows": 14,
    "duplicates_dropped": 20,
    "status": "success",
    "source_sha256": "f" * 64,
}

ARTIFACTS_STUB = {
    "rejects_csv": "/results/rejects_run-stub.csv",
    "validation_summary": "/results/validation_summary_run-stub.json",
    "quarantine_csv": "/quarantine/quarantine_run-stub.csv",
    "log_file": "/logs/etl_run-stub.log",
    "warehouse_db": "/warehouse.db",
    "quality_report_json": "/results/quality_report.json",
    "quality_report_md": "/results/quality_report.md",
}


def test_report_required_top_level_fields() -> None:
    report = build_report(SUMMARY_STUB, MANIFEST_STUB, ARTIFACTS_STUB)
    for key in (
        "schema_version",
        "generated_at",
        "run_id",
        "status",
        "data_provenance",
        "pipeline",
        "run",
        "source",
        "validation",
        "transform",
        "warehouse",
        "artifacts",
    ):
        assert key in report, f"report missing {key}"
    assert report["run_id"] == "run-stub"
    assert report["status"] == "completed"
    assert report["data_provenance"] == "synthetic"


def test_report_nested_validation_fields() -> None:
    report = build_report(SUMMARY_STUB, MANIFEST_STUB, ARTIFACTS_STUB)
    validation = report["validation"]
    assert validation["status"] == "fail"
    assert validation["rows_total"] == 420
    assert validation["rejected_rows"] == 36
    assert validation["error_violations"] == 53
    assert validation["warn_violations"] == 49
    assert validation["rules"][0]["id"] == "VR012"
    assert validation["rules"][0]["severity"] == "error"


def test_report_transform_and_warehouse_sections_with_manifest() -> None:
    report = build_report(SUMMARY_STUB, MANIFEST_STUB, ARTIFACTS_STUB)
    transform = report["transform"]
    assert transform["executed"] is True
    assert transform["rows_in"] == 420  # raw rows from validation
    assert transform["duplicates_dropped"] == 20
    assert transform["quarantined_rows"] == 36
    assert transform["rows_out"] == 364  # manifest.rows_in
    assert transform["rows_with_warnings"] == 14

    warehouse = report["warehouse"]
    assert warehouse["executed"] is True
    assert warehouse["table"] == "readings"
    assert warehouse["table_rows"] == 364
    assert warehouse["manifest"]["rejects"] == 36


def test_report_without_manifest_is_marked_not_executed() -> None:
    report = build_report(SUMMARY_STUB, None, ARTIFACTS_STUB)
    assert report["status"] == "aborted_after_validation"
    assert report["transform"]["executed"] is False
    assert report["warehouse"]["executed"] is False
    assert "abort_on_error" in report["transform"]["reason"]


def test_report_validated_only_status_when_validation_passes() -> None:
    passed = dict(SUMMARY_STUB)
    passed["validation"] = dict(SUMMARY_STUB["validation"], status="pass")
    report = build_report(passed, None, ARTIFACTS_STUB)
    assert report["status"] == "validated_only"


def test_markdown_rendering_contains_sections() -> None:
    report = build_report(SUMMARY_STUB, MANIFEST_STUB, ARTIFACTS_STUB)
    markdown = render_markdown(report)
    assert markdown.startswith("# Data Quality Report — run-stub")
    for heading in (
        "## Run metadata",
        "## Source files",
        "## Validation results",
        "## Transform results",
        "## Warehouse",
        "## Artifacts",
    ):
        assert heading in markdown
    assert "VR012" in markdown
    assert "synthetic" in markdown


def test_write_report_creates_both_files(tmp_path: Path) -> None:
    report = build_report(SUMMARY_STUB, MANIFEST_STUB, ARTIFACTS_STUB)
    json_path, md_path = write_report(report, tmp_path / "results")

    assert json_path.is_file() and md_path.is_file()
    reloaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert reloaded["run_id"] == "run-stub"
    assert "## Warehouse" in md_path.read_text(encoding="utf-8")


def test_full_run_produces_report_with_real_counts(make_config) -> None:
    """End-to-end: run the pipeline on the 420-row fixture and inspect output."""
    config = load_config(make_config())
    result = run_pipeline(config, "run-report-test")

    assert result.exit_code == 0
    assert result.report_json is not None and result.report_md is not None

    report = json.loads(result.report_json.read_text(encoding="utf-8"))
    assert report["run_id"] == "run-report-test"
    assert report["status"] == "completed"
    assert report["source"]["rows_read"] == 420
    assert report["validation"]["rejected_rows"] == 36
    assert report["transform"]["duplicates_dropped"] == 20
    assert report["transform"]["rows_out"] == 364
    assert report["warehouse"]["table_rows"] == 364
    assert report["artifacts"]["rejects_csv"].endswith("rejects_run-report-test.csv")

    markdown = result.report_md.read_text(encoding="utf-8")
    assert "run-report-test" in markdown
    assert "## Validation results" in markdown
