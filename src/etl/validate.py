"""Validate stage: a declarative rule engine.

The rule catalogue lives in ``config/validation_rules.json`` so data-quality
decisions are data, not code: adding a rule never requires touching the
pipeline. Each rule declares an ``id``, a ``description``, a ``field``, a
``severity`` (``error`` or ``warn``) and a ``predicate``.

Predicate types (all evaluated as vectorised pandas pass-masks where
``True`` = the row satisfies the rule):

=============== ==========================================================
Type            Meaning
=============== ==========================================================
``not_null``    value is present and not blank/whitespace
``numeric``     value coerces to a number
``range``       numeric value within inclusive ``min``/``max`` bounds
``iso_datetime``strict ``YYYY-MM-DDTHH:MM:SS`` timestamp that parses
``not_future``  parseable timestamp is not after "now"
``in_set``      value is in a literal ``values`` list or a named context set
``unique``      row is not an exact duplicate of an earlier row
=============== ==========================================================

Predicates **fail closed**: an unparseable value never satisfies ``numeric``,
``range``, ``iso_datetime`` or ``not_future``. The engine is deterministic and
side-effect free; writing the rejects CSV is a separate helper.

Semantics:

* any ``error``-severity violation marks the row for quarantine and flips the
  overall status to ``fail``;
* ``warn``-severity violations only flag the row (``warning_flags`` column).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

#: Severities a rule may declare.
SEVERITIES: frozenset[str] = frozenset({"error", "warn"})

#: Predicate types the engine knows how to evaluate.
SUPPORTED_PREDICATES: frozenset[str] = frozenset(
    {"not_null", "numeric", "range", "iso_datetime", "not_future", "in_set", "unique"}
)

_ISO_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


class RuleSpecError(Exception):
    """Raised when the rule catalogue itself is invalid (bad config)."""


@dataclass(frozen=True)
class Rule:
    """One validation rule from the catalogue.

    Attributes:
        id: Stable identifier, e.g. ``VR008``.
        description: Human-readable explanation shown in reports.
        field: Column the rule applies to (``None`` for ``unique`` rules).
        severity: ``error`` (quarantine) or ``warn`` (flag).
        predicate: Declarative predicate specification.
    """

    id: str
    description: str
    field: str | None
    severity: str
    predicate: dict[str, Any]

    @property
    def predicate_type(self) -> str:
        """The predicate type string, e.g. ``range``."""
        return str(self.predicate["type"])

    @property
    def target_fields(self) -> list[str]:
        """Columns touched by this rule (for the ``unique`` predicate: all)."""
        if self.predicate_type == "unique":
            return list(self.predicate.get("fields") or [])
        return [str(self.field)] if self.field is not None else []


@dataclass
class ValidationOutcome:
    """Result of running every rule over a frame.

    Attributes:
        rows_total: Number of rows that were validated.
        rule_counts: Violation count per rule id (all rules, including zeros).
        error_flags: Row index -> error-severity rule ids that fired.
        warn_flags: Row index -> warn-severity rule ids that fired.
        status: ``fail`` when any error-severity violation exists, else ``pass``.
        evaluated_at: ISO timestamp of when validation ran.
    """

    rows_total: int
    rule_counts: dict[str, int]
    error_flags: dict[int, list[str]] = dataclass_field(default_factory=dict)
    warn_flags: dict[int, list[str]] = dataclass_field(default_factory=dict)
    status: str = "pass"
    evaluated_at: str = ""

    @property
    def rejected_count(self) -> int:
        """Rows carrying at least one error-severity violation."""
        return len(self.error_flags)

    @property
    def flagged_count(self) -> int:
        """Rows carrying at least one warn-severity violation."""
        return len(self.warn_flags)

    def total_violations(self, rules: Sequence[Rule], severity: str) -> int:
        """Sum of per-rule counts for rules of ``severity``."""
        return sum(
            self.rule_counts.get(rule.id, 0)
            for rule in rules
            if rule.severity == severity
        )

    def to_summary(self, rules: Sequence["Rule"]) -> dict[str, Any]:
        """JSON-serialisable validation summary (used by report + artifacts)."""
        return {
            "status": self.status,
            "rows_total": self.rows_total,
            "evaluated_at": self.evaluated_at,
            "rules_evaluated": len(rules),
            "error_violations": self.total_violations(rules, "error"),
            "warn_violations": self.total_violations(rules, "warn"),
            "rejected_rows": self.rejected_count,
            "flagged_rows": self.flagged_count,
            "rules": [
                {
                    "id": rule.id,
                    "description": rule.description,
                    "field": rule.field,
                    "severity": rule.severity,
                    "predicate": rule.predicate,
                    "violations": self.rule_counts.get(rule.id, 0),
                }
                for rule in rules
            ],
        }


# --------------------------------------------------------------------------- #
# Rule catalogue loading
# --------------------------------------------------------------------------- #


def load_rules(path: str | Path) -> list[Rule]:
    """Load and structurally validate the JSON rule catalogue.

    Args:
        path: Path to ``validation_rules.json``.

    Returns:
        Rules in catalogue order.

    Raises:
        RuleSpecError: The file is missing/invalid or a rule is malformed
            (duplicate ids, unknown severity, unsupported predicate, ...).
    """
    rules_path = Path(path)
    if not rules_path.is_file():
        raise RuleSpecError(f"validation rules file not found: {rules_path}")
    try:
        raw = json.loads(rules_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuleSpecError(f"validation rules file is not valid JSON: {exc}") from exc

    entries = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise RuleSpecError("validation rules file must contain a non-empty 'rules' list")

    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise RuleSpecError(f"rule #{position} must be a JSON object")
        rule_id = entry.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise RuleSpecError(f"rule #{position} is missing a string 'id'")
        if rule_id in seen_ids:
            raise RuleSpecError(f"duplicate rule id: {rule_id}")
        severity = entry.get("severity")
        if severity not in SEVERITIES:
            raise RuleSpecError(
                f"rule {rule_id}: severity must be one of {sorted(SEVERITIES)}, got {severity!r}"
            )
        predicate = entry.get("predicate")
        if not isinstance(predicate, dict) or "type" not in predicate:
            raise RuleSpecError(f"rule {rule_id}: 'predicate' must be an object with a 'type'")
        predicate_type = predicate["type"]
        if predicate_type not in SUPPORTED_PREDICATES:
            raise RuleSpecError(
                f"rule {rule_id}: unsupported predicate type {predicate_type!r} "
                f"(supported: {', '.join(sorted(SUPPORTED_PREDICATES))})"
            )
        rule_field = entry.get("field")
        if rule_field is not None and not isinstance(rule_field, str):
            raise RuleSpecError(f"rule {rule_id}: 'field' must be a string or null")
        if predicate_type != "unique" and not rule_field:
            raise RuleSpecError(f"rule {rule_id}: predicate {predicate_type!r} requires a 'field'")
        if predicate_type == "range":
            bounds = [b for b in ("min", "max") if b in predicate]
            if not bounds:
                raise RuleSpecError(f"rule {rule_id}: range predicate needs 'min' and/or 'max'")
        if predicate_type == "in_set":
            if "values" not in predicate and "source" not in predicate:
                raise RuleSpecError(f"rule {rule_id}: in_set predicate needs 'values' or 'source'")
        description = entry.get("description")
        if not isinstance(description, str) or not description:
            raise RuleSpecError(f"rule {rule_id}: missing 'description'")

        seen_ids.add(rule_id)
        rules.append(
            Rule(
                id=rule_id,
                description=description,
                field=rule_field,
                severity=severity,
                predicate=predicate,
            )
        )
    logger.debug("validate.rules_loaded count=%d path=%s", len(rules), rules_path)
    return rules


# --------------------------------------------------------------------------- #
# Predicates (each returns a boolean pass-mask aligned to the frame index)
# --------------------------------------------------------------------------- #

PredicateFn = Callable[[pd.DataFrame, Rule, "Mapping[str, Iterable[str]]", datetime], pd.Series]


def _p_not_null(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del context, now
    values = frame[str(rule.field)].astype(str)
    return values.str.strip() != ""


def _p_numeric(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del context, now
    return pd.to_numeric(frame[str(rule.field)], errors="coerce").notna()


def _p_range(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del context, now
    numeric = pd.to_numeric(frame[str(rule.field)], errors="coerce")
    mask = numeric.notna()
    if "min" in rule.predicate:
        mask = mask & (numeric >= rule.predicate["min"])
    if "max" in rule.predicate:
        mask = mask & (numeric <= rule.predicate["max"])
    return mask.fillna(False).astype(bool)


def _p_iso_datetime(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del context, now
    values = frame[str(rule.field)].astype(str)
    syntax_ok = values.str.fullmatch(_ISO_PATTERN).fillna(False).astype(bool)
    parsed = pd.to_datetime(values, format=_ISO_FORMAT, errors="coerce")
    return syntax_ok & parsed.notna()


def _p_not_future(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del context
    parsed = pd.to_datetime(frame[str(rule.field)], format=_ISO_FORMAT, errors="coerce")
    return (parsed.notna() & (parsed <= now)).fillna(False).astype(bool)


def _p_in_set(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del now
    predicate = rule.predicate
    if "values" in predicate:
        allowed = set(predicate["values"])
    else:
        key = str(predicate.get("source", "")).removeprefix("context.")
        if key not in context:
            raise RuleSpecError(
                f"rule {rule.id}: context source {key!r} was not provided to the engine"
            )
        allowed = set(context[key])
    return frame[str(rule.field)].isin(allowed)


def _p_unique(frame: pd.DataFrame, rule: Rule, context: Mapping[str, Iterable[str]], now: datetime) -> pd.Series:
    del context, now
    subset = rule.predicate.get("fields") or list(frame.columns)
    return ~frame.duplicated(subset=list(subset), keep="first")


_PREDICATES: dict[str, PredicateFn] = {
    "not_null": _p_not_null,
    "numeric": _p_numeric,
    "range": _p_range,
    "iso_datetime": _p_iso_datetime,
    "not_future": _p_not_future,
    "in_set": _p_in_set,
    "unique": _p_unique,
}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def validate_frame(
    frame: pd.DataFrame,
    rules: Sequence[Rule],
    context: Mapping[str, Iterable[str]] | None = None,
    now: datetime | None = None,
) -> ValidationOutcome:
    """Evaluate every rule against every row.

    Args:
        frame: Raw frame (string-typed columns).
        rules: Rule catalogue, usually from :func:`load_rules`.
        context: Named value sets referenced by ``in_set`` predicates
            (e.g. ``{"known_device_ids": [...]}``).
        now: Reference clock for ``not_future`` (defaults to wall clock).

    Returns:
        :class:`ValidationOutcome` with per-rule counts, per-row flags and the
        overall pass/fail status.

    Raises:
        RuleSpecError: A rule references a column the frame does not have, or
            an ``in_set`` predicate names a missing context source.
    """
    context = dict(context or {})
    reference_time = now if now is not None else datetime.now()

    error_flags: dict[int, list[str]] = {}
    warn_flags: dict[int, list[str]] = {}
    rule_counts: dict[str, int] = {}

    for rule in rules:
        if rule.predicate_type != "unique" and rule.field is not None and rule.field not in frame.columns:
            raise RuleSpecError(f"rule {rule.id}: unknown field {rule.field!r} (frame has {list(frame.columns)})")

        passed = _PREDICATES[rule.predicate_type](frame, rule, context, reference_time)
        failed_mask = ~passed.astype(bool)
        count = int(failed_mask.sum())
        rule_counts[rule.id] = count
        if count:
            bucket = error_flags if rule.severity == "error" else warn_flags
            for index in frame.index[failed_mask]:
                bucket.setdefault(int(index), []).append(rule.id)

    outcome = ValidationOutcome(
        rows_total=len(frame),
        rule_counts=rule_counts,
        error_flags=error_flags,
        warn_flags=warn_flags,
        status="fail" if error_flags else "pass",
        evaluated_at=datetime.now().isoformat(timespec="seconds"),
    )
    logger.info(
        "validate.done rows=%d status=%s rejected_rows=%d flagged_rows=%d "
        "error_violations=%d warn_violations=%d",
        outcome.rows_total,
        outcome.status,
        outcome.rejected_count,
        outcome.flagged_count,
        outcome.total_violations(rules, "error"),
        outcome.total_violations(rules, "warn"),
    )
    return outcome


def write_rejects(frame: pd.DataFrame, outcome: ValidationOutcome, path: Path) -> int:
    """Write the error-level rejects CSV.

    Columns: ``source_row`` (0-based raw row index), every original column,
    then ``error_rules`` and ``warn_rules`` (semicolon-separated rule ids).

    Args:
        frame: The raw frame that was validated.
        outcome: Result returned by :func:`validate_frame`.
        path: Destination CSV path (parent directories are created).

    Returns:
        Number of reject rows written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    indices = sorted(outcome.error_flags)
    rejects = frame.loc[indices].copy()
    rejects.insert(0, "source_row", indices)
    rejects["error_rules"] = [";".join(outcome.error_flags[i]) for i in indices]
    rejects["warn_rules"] = [";".join(outcome.warn_flags.get(i, [])) for i in indices]
    rejects.to_csv(path, index=False)
    logger.info("validate.rejects_written rows=%d path=%s", len(rejects), path)
    return len(rejects)
