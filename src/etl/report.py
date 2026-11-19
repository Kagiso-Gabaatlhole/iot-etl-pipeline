"""Report stage: data-quality report as JSON + Markdown.

The report is rebuilt from two artifacts that every run leaves behind:

* ``results/validation_summary_<run_id>.json`` -- written by the validate
  stage (source checksums, per-rule counts, pass/fail status), and
* the ``load_manifest`` row in the warehouse (rows in/out, rejects,
  timestamps) -- present only if the run reached the load stage.

:func:`build_report` is a pure function of those two inputs, which is why the
``etl report`` command can regenerate ``results/quality_report.json`` and
``results/quality_report.md`` after the fact without re-running the pipeline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Bump when the report layout changes.
REPORT_SCHEMA_VERSION = 1


class ReportError(Exception):
    """Raised when a report cannot be built (e.g. missing run artifacts)."""


# --------------------------------------------------------------------------- #
# Artifact paths (shared with the orchestrator)
# --------------------------------------------------------------------------- #


def rejects_path(results_dir: Path, run_id: str) -> Path:
    """Path of the error-level rejects CSV for ``run_id``."""
    return Path(results_dir) / f"rejects_{run_id}.csv"


def summary_path(results_dir: Path, run_id: str) -> Path:
    """Path of the validation summary JSON for ``run_id``."""
    return Path(results_dir) / f"validation_summary_{run_id}.json"


def quarantine_path(quarantine_dir: Path, run_id: str) -> Path:
    """Path of the quarantine CSV for ``run_id``."""
    return Path(quarantine_dir) / f"quarantine_{run_id}.csv"


def log_path(logs_dir: Path, run_id: str) -> Path:
    """Path of the per-run log file for ``run_id``."""
    return Path(logs_dir) / f"etl_{run_id}.log"


def write_validation_summary(summary: dict[str, Any], path: Path) -> Path:
    """Persist the validation summary JSON (pretty-printed, UTF-8)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    logger.debug("report.summary_written path=%s", path)
    return path


def load_validation_summary(results_dir: Path, run_id: str) -> dict[str, Any]:
    """Load a previously written validation summary.

    Raises:
        ReportError: The summary file for ``run_id`` does not exist or is not
            valid JSON (the CLI maps this to exit code 2).
    """
    path = summary_path(results_dir, run_id)
    if not path.is_file():
        raise ReportError(
            f"validation summary not found for run '{run_id}': {path} "
            "(run `python -m etl run` or `python -m etl validate` first)"
        )
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"validation summary is not valid JSON: {path} ({exc})") from exc
    if not isinstance(loaded, dict):
        raise ReportError(f"validation summary must be a JSON object: {path}")
    return loaded


# --------------------------------------------------------------------------- #
# Report construction
# --------------------------------------------------------------------------- #


def build_report(
    summary: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    artifacts: Mapping[str, str | None],
) -> dict[str, Any]:
    """Build the full data-quality report (pure function).

    Args:
        summary: Validation summary produced by the validate stage.
        manifest: ``load_manifest`` row for this run, or ``None`` when the run
            never reached the load stage.
        artifacts: Paths of the run's artifacts (rejects CSV, quarantine CSV,
            log file, summary, final report files, warehouse database).

    Returns:
        JSON-serialisable report dictionary.
    """
    run_id = str(summary["run_id"])
    validation = dict(summary["validation"])

    if manifest is not None:
        status = "completed"
    elif validation.get("status") == "fail":
        status = "aborted_after_validation"
    else:
        status = "validated_only"

    started_at = str(summary.get("started_at", ""))
    finished_at = (
        str(manifest["finished_at"]) if manifest is not None else str(validation.get("evaluated_at", ""))
    )

    if manifest is not None:
        transform_section: dict[str, Any] = {
            "executed": True,
            "rows_in": int(validation["rows_total"]),
            "duplicates_dropped": int(manifest["duplicates_dropped"]),
            "quarantined_rows": int(manifest["quarantined_rows"]),
            "rows_out": int(manifest["rows_in"]),
            "rows_with_warnings": int(manifest["flagged_rows"]),
        }
        warehouse_section: dict[str, Any] = {
            "executed": True,
            "database": artifacts.get("warehouse_db"),
            "table": "readings",
            "table_rows": int(manifest["rows_out"]),
            "manifest": dict(manifest),
        }
    else:
        if status == "aborted_after_validation":
            reason = "not executed: validation failed and validation.abort_on_error=true"
        else:
            reason = "not executed: no load manifest recorded for this run"
        transform_section = {"executed": False, "reason": reason}
        warehouse_section = {"executed": False, "reason": reason}

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "status": status,
        "data_provenance": "synthetic",
        "pipeline": dict(summary.get("pipeline", {})),
        "run": {
            "started_at": started_at,
            "finished_at": finished_at,
            "config_path": summary.get("pipeline", {}).get("config_path"),
            "abort_on_error": bool(summary.get("abort_on_error", False)),
        },
        "source": {
            "files": list(summary.get("source_files", [])),
            "rows_read": int(validation["rows_total"]),
        },
        "validation": validation,
        "transform": transform_section,
        "warehouse": warehouse_section,
        "artifacts": {key: value for key, value in artifacts.items()},
    }


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Render a GitHub-flavoured Markdown table."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report dictionary as a Markdown document."""
    lines: list[str] = []
    lines.append(f"# Data Quality Report — {report['run_id']}")
    lines.append("")
    lines.append(f"- **Status:** `{report['status']}`")
    lines.append(f"- **Generated:** {report.get('generated_at', '')}")
    pipeline = report.get("pipeline", {})
    lines.append(f"- **Pipeline:** {pipeline.get('pipeline_name', 'iot-telemetry-etl')} "
                 f"v{pipeline.get('version', '?')}")
    lines.append("- **Data provenance:** synthetic (generated by "
                 "`src/pipeline/generate_data.py`, seed 42)")
    lines.append("")

    run = report.get("run", {})
    lines.append("## Run metadata")
    lines.append("")
    lines.extend(
        _table(
            ["Field", "Value"],
            [
                ("run_id", report["run_id"]),
                ("started_at", run.get("started_at", "")),
                ("finished_at", run.get("finished_at", "")),
                ("config_path", run.get("config_path", "")),
                ("abort_on_error", str(run.get("abort_on_error", False)).lower()),
            ],
        )
    )
    lines.append("")

    source = report.get("source", {})
    lines.append("## Source files")
    lines.append("")
    source_rows = [
        (
            Path(str(item.get("path", ""))).name,
            item.get("row_count", 0),
            str(item.get("sha256", ""))[:16] + "…",
        )
        for item in source.get("files", [])
    ]
    lines.extend(_table(["File", "Rows", "SHA-256 (first 16)"], source_rows))
    lines.append("")
    lines.append(f"Raw telemetry rows read: **{source.get('rows_read', 0)}**")
    lines.append("")

    validation = report.get("validation", {})
    lines.append("## Validation results")
    lines.append("")
    lines.append(
        f"Overall status: **{validation.get('status', '?')}** — "
        f"{validation.get('error_violations', 0)} error-level violations across "
        f"{validation.get('rejected_rows', 0)} rows (quarantined), "
        f"{validation.get('warn_violations', 0)} warn-level violations across "
        f"{validation.get('flagged_rows', 0)} rows (flagged). "
        f"Rules evaluated: {validation.get('rules_evaluated', 0)}."
    )
    lines.append("")
    lines.extend(
        _table(
            ["Rule", "Severity", "Field", "Violations", "Description"],
            [
                (
                    rule.get("id", ""),
                    rule.get("severity", ""),
                    rule.get("field") or "(all columns)",
                    rule.get("violations", 0),
                    rule.get("description", ""),
                )
                for rule in validation.get("rules", [])
            ],
        )
    )
    lines.append("")

    transform = report.get("transform", {})
    lines.append("## Transform results")
    lines.append("")
    if transform.get("executed"):
        lines.extend(
            _table(
                ["Metric", "Rows"],
                [
                    ("Rows read (raw)", transform.get("rows_in", 0)),
                    ("Exact duplicates dropped", transform.get("duplicates_dropped", 0)),
                    ("Rows quarantined (error severity)", transform.get("quarantined_rows", 0)),
                    ("Rows handed to the warehouse (clean)", transform.get("rows_out", 0)),
                    ("Clean rows carrying warnings", transform.get("rows_with_warnings", 0)),
                ],
            )
        )
    else:
        lines.append(f"_{transform.get('reason', 'not executed')}_")
    lines.append("")

    warehouse = report.get("warehouse", {})
    lines.append("## Warehouse")
    lines.append("")
    if warehouse.get("executed"):
        manifest = warehouse.get("manifest", {})
        lines.extend(
            _table(
                ["Field", "Value"],
                [
                    ("Database", warehouse.get("database", "")),
                    ("Table", warehouse.get("table", "")),
                    ("Rows after load", warehouse.get("table_rows", 0)),
                    ("manifest.rows_in (clean rows loaded)", manifest.get("rows_in", 0)),
                    ("manifest.rows_out (table rows after load)", manifest.get("rows_out", 0)),
                    ("manifest.rejects (error rows)", manifest.get("rejects", 0)),
                    ("manifest.warnings (warn violations)", manifest.get("warnings", 0)),
                    ("manifest.duplicates_dropped", manifest.get("duplicates_dropped", 0)),
                    ("manifest.source_sha256", str(manifest.get("source_sha256", ""))[:16] + "…"),
                ],
            )
        )
    else:
        lines.append(f"_{warehouse.get('reason', 'not executed')}_")
    lines.append("")

    lines.append("## Artifacts")
    lines.append("")
    artifact_rows = [
        (key, value if value else "—")
        for key, value in report.get("artifacts", {}).items()
    ]
    lines.extend(_table(["Artifact", "Path"], artifact_rows))
    lines.append("")
    lines.append(
        "> **Note:** all input data is synthetic and intentionally contains "
        "quality problems (duplicates, missing values, out-of-range readings, "
        "future timestamps, malformed rows, unknown device ids). See the "
        "repository README for provenance and limitations."
    )
    lines.append("")
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], results_dir: Path) -> tuple[Path, Path]:
    """Write ``quality_report.json`` and ``quality_report.md``.

    Args:
        report: Report dictionary from :func:`build_report`.
        results_dir: Directory receiving both files (created if missing).

    Returns:
        ``(json_path, markdown_path)``
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / "quality_report.json"
    md_path = results_dir / "quality_report.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    logger.info("report.written json=%s md=%s", json_path, md_path)
    return json_path, md_path
