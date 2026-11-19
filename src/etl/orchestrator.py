"""Orchestrator: wires the five stages together and maps outcomes to exit codes.

Exit-code contract (see also the CLI):

* ``0`` -- the command completed successfully,
* ``1`` -- validation found error-severity violations in a context where they
  are fatal: the ``validate`` command, or ``run`` with
  ``validation.abort_on_error=true`` (the quality report is still written),
* ``2`` -- configuration/usage problem or an unexpected error.

Each stage remains independently testable: this module only sequences calls
and persists artifacts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from etl.config import PipelineConfig
from etl.extract import ExtractResult, extract
from etl.load import LoadManifest, fetch_manifest, latest_manifest, load
from etl.report import (
    ReportError,
    build_report,
    load_validation_summary,
    log_path,
    quarantine_path,
    rejects_path,
    summary_path,
    write_report,
    write_validation_summary,
)
from etl.transform import transform, write_quarantine
from etl.validate import (
    Rule,
    ValidationOutcome,
    load_rules,
    validate_frame,
    write_rejects,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineResult:
    """Outcome of a CLI command.

    Attributes:
        run_id: Identifier of this invocation.
        command: ``run``, ``validate`` or ``report``.
        exit_code: Process exit code (0 success, 1 validation failed, 2 error).
        status: Human-readable status string.
        summary_path: Validation summary JSON, when one was written.
        report_json: Quality report JSON path, when a report was written.
        report_md: Quality report Markdown path, when a report was written.
    """

    run_id: str
    command: str
    exit_code: int
    status: str
    summary_path: Path | None = None
    report_json: Path | None = None
    report_md: Path | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _validate(
    config: PipelineConfig, run_id: str, started_at: str
) -> tuple[ExtractResult, list[Rule], ValidationOutcome, dict[str, Any]]:
    """Run extract + validate and persist the rejects CSV + summary JSON."""
    extraction = extract(config.readings_csv, config.device_registry_csv)
    rules = load_rules(config.validation_rules)
    context = {"known_device_ids": extraction.device_registry["device_id"].tolist()}
    outcome = validate_frame(extraction.readings, rules, context)

    write_rejects(extraction.readings, outcome, rejects_path(config.results_dir, run_id))

    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "pipeline": {
            "name": config.pipeline_name,
            "version": config.version,
            "config_path": str(config.config_path),
        },
        "abort_on_error": config.abort_on_error,
        "source_files": [file_meta.to_dict() for file_meta in extraction.files],
        "validation": outcome.to_summary(rules),
    }
    write_validation_summary(summary, summary_path(config.results_dir, run_id))
    return extraction, rules, outcome, summary


def _artifacts(
    config: PipelineConfig, run_id: str, *, include_quarantine: bool
) -> dict[str, str | None]:
    """Collect the artifact paths referenced by the quality report."""
    quarantine_file = quarantine_path(config.quarantine_dir, run_id)
    return {
        "rejects_csv": str(rejects_path(config.results_dir, run_id)),
        "validation_summary": str(summary_path(config.results_dir, run_id)),
        "quarantine_csv": str(quarantine_file) if include_quarantine and quarantine_file.is_file() else None,
        "log_file": str(log_path(config.logs_dir, run_id)),
        "warehouse_db": str(config.warehouse_db),
        "quality_report_json": str(config.results_dir / "quality_report.json"),
        "quality_report_md": str(config.results_dir / "quality_report.md"),
    }


def run_validation(config: PipelineConfig, run_id: str) -> PipelineResult:
    """Execute extract + validate only.

    Writes the rejects CSV and validation summary, then returns exit code 1
    when any error-severity rule fired (0 otherwise).
    """
    started_at = _now()
    logger.info(
        "run.start command=validate run_id=%s config=%s", run_id, config.config_path
    )
    _, rules, outcome, _ = _validate(config, run_id, started_at)
    exit_code = 1 if outcome.status == "fail" else 0
    status = "validation_failed" if exit_code else "validation_passed"
    logger.log(
        logging.ERROR if exit_code else logging.INFO,
        "run.finish command=validate run_id=%s status=%s exit_code=%d rules=%d",
        run_id, status, exit_code, len(rules),
    )
    return PipelineResult(
        run_id=run_id,
        command="validate",
        exit_code=exit_code,
        status=status,
        summary_path=summary_path(config.results_dir, run_id),
    )


def run_pipeline(config: PipelineConfig, run_id: str) -> PipelineResult:
    """Execute the full pipeline: extract -> validate -> transform -> load -> report.

    When validation finds error-severity violations and
    ``validation.abort_on_error`` is true, the run stops after validation, the
    quality report is still written, and the result carries exit code 1.
    """
    started_at = _now()
    logger.info("run.start command=run run_id=%s config=%s", run_id, config.config_path)

    extraction, rules, outcome, summary = _validate(config, run_id, started_at)

    if outcome.status == "fail" and config.abort_on_error:
        report = build_report(summary, None, _artifacts(config, run_id, include_quarantine=False))
        report_json, report_md = write_report(report, config.results_dir)
        logger.error(
            "run.aborted reason=error_severity_violations rejected_rows=%d "
            "abort_on_error=true exit_code=1",
            outcome.rejected_count,
        )
        return PipelineResult(
            run_id=run_id,
            command="run",
            exit_code=1,
            status="aborted_after_validation",
            summary_path=summary_path(config.results_dir, run_id),
            report_json=report_json,
            report_md=report_md,
        )

    transform_result = transform(extraction.readings, outcome, extraction.device_registry)
    write_quarantine(
        extraction.readings, outcome, quarantine_path(config.quarantine_dir, run_id)
    )

    warn_violations = outcome.total_violations(rules, "warn")
    manifest = LoadManifest(
        run_id=run_id,
        started_at=started_at,
        finished_at=_now(),
        rows_in=transform_result.rows_out,
        rejects=outcome.rejected_count,
        quarantined_rows=transform_result.quarantined_rows,
        warnings=warn_violations,
        flagged_rows=transform_result.flagged_rows,
        duplicates_dropped=transform_result.duplicates_dropped,
        status="success",
        source_sha256=extraction.readings_meta.sha256,
    )
    load(transform_result.frame, manifest, config.warehouse_db)
    manifest_row = fetch_manifest(config.warehouse_db, run_id)
    if manifest_row is None:  # pragma: no cover - defensive
        raise ReportError(f"manifest row for run '{run_id}' disappeared after load")

    report = build_report(summary, manifest_row, _artifacts(config, run_id, include_quarantine=True))
    report_json, report_md = write_report(report, config.results_dir)

    logger.info(
        "run.finish command=run run_id=%s status=completed exit_code=0 "
        "rows_in=%d rows_out=%d rejects=%d warnings=%d",
        run_id,
        transform_result.rows_in,
        manifest_row["rows_out"],
        outcome.rejected_count,
        warn_violations,
    )
    return PipelineResult(
        run_id=run_id,
        command="run",
        exit_code=0,
        status="completed",
        summary_path=summary_path(config.results_dir, run_id),
        report_json=report_json,
        report_md=report_md,
    )


def regenerate_report(config: PipelineConfig, run_id: str | None) -> PipelineResult:
    """Rebuild ``results/quality_report.{json,md}`` from stored artifacts.

    Args:
        config: Active pipeline configuration.
        run_id: Run to report on. ``None`` selects the most recently loaded
            run from the warehouse manifest.

    Raises:
        ReportError: No run could be resolved or its validation summary is
            missing (the CLI maps this to exit code 2).
    """
    resolved_run_id = run_id
    manifest: dict[str, Any] | None = None

    if resolved_run_id is None:
        manifest = latest_manifest(config.warehouse_db)
        if manifest is None:
            raise ReportError(
                f"no load manifest found in {config.warehouse_db}; "
                "run `python -m etl run --config config/pipeline_config.json` first"
            )
        resolved_run_id = str(manifest["run_id"])
    else:
        manifest = fetch_manifest(config.warehouse_db, resolved_run_id)

    summary = load_validation_summary(config.results_dir, resolved_run_id)
    report = build_report(summary, manifest, _artifacts(config, resolved_run_id, include_quarantine=manifest is not None))
    report_json, report_md = write_report(report, config.results_dir)

    logger.info(
        "run.finish command=report run_id=%s status=%s exit_code=0",
        resolved_run_id, report["status"],
    )
    return PipelineResult(
        run_id=str(resolved_run_id),
        command="report",
        exit_code=0,
        status=str(report["status"]),
        summary_path=summary_path(config.results_dir, str(resolved_run_id)),
        report_json=report_json,
        report_md=report_md,
    )
