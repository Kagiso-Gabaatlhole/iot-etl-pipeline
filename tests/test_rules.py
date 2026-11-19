"""Rule-engine correctness tests.

Each case proves that one catalogue rule catches exactly the rows it is meant
to catch (and nothing else): a clean row plus one deliberately broken row.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

from etl.validate import (
    Rule,
    RuleSpecError,
    load_rules,
    validate_frame,
    write_rejects,
)

# now is pinned so the `not_future` rule tests are deterministic.
NOW = datetime(2026, 10, 1, 12, 0, 0)

# (overrides for the broken row, expected error rules, expected warn rules)
CASES: list[tuple[dict[str, str], set[str], set[str]]] = [
    ({}, set(), set()),                                             # clean baseline
    ({"reading_id": ""}, {"VR001"}, set()),                         # missing required id
    ({"device_id": ""}, {"VR002", "VR006"}, set()),                 # blank device + unknown
    ({"timestamp": ""}, {"VR003", "VR004", "VR005"}, set()),        # blank timestamp
    ({"timestamp": "not-a-date"}, {"VR004", "VR005"}, set()),       # malformed timestamp
    ({"timestamp": "2031-05-01T00:00:00"}, {"VR005"}, set()),       # future timestamp
    ({"device_id": "DEV999"}, {"VR006"}, set()),                    # unknown device id
    ({"temperature_c": "abc"}, {"VR007", "VR008"}, {"VR013"}),      # wrong type (fail closed)
    ({"temperature_c": "75.0"}, {"VR008"}, {"VR013"}),              # impossible temperature
    ({"temperature_c": "46.5"}, set(), {"VR013"}),                  # elevated -> warn only
    ({"humidity_pct": "warm"}, {"VR009", "VR010"}, {"VR014"}),      # wrong type (fail closed)
    ({"humidity_pct": "115.0"}, {"VR010"}, {"VR014"}),              # > 100 percent
    ({"humidity_pct": "96.0"}, set(), {"VR014"}),                   # high but plausible
    ({"power_kw": "-3.0"}, {"VR012"}, set()),                       # negative power
    ({"power_kw": "N/A"}, {"VR011", "VR012"}, set()),               # wrong type
    ({"power_kw": "25.0"}, {"VR012"}, set()),                       # above 15 kW
]


@pytest.mark.parametrize(
    ("overrides", "expected_errors", "expected_warns"),
    CASES,
    ids=[f"row-{index}" for index in range(len(CASES))],
)
def test_each_rule_catches_its_bad_row(
    make_row: Callable[..., dict[str, str]],
    rule_catalogue: list[Rule],
    known_device_ids: list[str],
    overrides: dict[str, str],
    expected_errors: set[str],
    expected_warns: set[str],
) -> None:
    clean = make_row()
    # A distinct reading_id keeps the broken row from tripping the duplicate
    # rule, so each case only reports the rules it targets.
    broken = make_row(**({"reading_id": "R0000002"} | overrides))
    frame = pd.DataFrame([clean, broken], columns=list(clean.keys()))

    outcome = validate_frame(
        frame,
        rule_catalogue,
        context={"known_device_ids": known_device_ids},
        now=NOW,
    )

    assert set(outcome.error_flags.get(1, [])) == expected_errors
    assert set(outcome.warn_flags.get(1, [])) == expected_warns
    assert 0 not in outcome.error_flags, "clean row must not be rejected"
    assert 0 not in outcome.warn_flags, "clean row must not be flagged"
    assert outcome.status == ("fail" if expected_errors else "pass")
    assert outcome.rejected_count == (1 if expected_errors else 0)


def test_unique_rule_flags_only_later_occurrences(
    make_row: Callable[..., dict[str, str]],
    rule_catalogue: list[Rule],
    known_device_ids: list[str],
) -> None:
    row = make_row()
    frame = pd.DataFrame([row, dict(row), dict(row)], columns=list(row.keys()))
    outcome = validate_frame(
        frame, rule_catalogue, context={"known_device_ids": known_device_ids}, now=NOW
    )
    assert 0 not in outcome.warn_flags
    assert outcome.warn_flags.get(1) == ["VR015"]
    assert outcome.warn_flags.get(2) == ["VR015"]
    assert outcome.status == "pass"  # duplicates are warn-level only


def test_rule_counts_are_reported_for_every_rule(
    make_row: Callable[..., dict[str, str]],
    rule_catalogue: list[Rule],
    known_device_ids: list[str],
) -> None:
    frame = pd.DataFrame([make_row()], columns=list(make_row().keys()))
    outcome = validate_frame(
        frame, rule_catalogue, context={"known_device_ids": known_device_ids}, now=NOW
    )
    assert set(outcome.rule_counts) == {rule.id for rule in rule_catalogue}
    assert outcome.rule_counts["VR015"] == 0
    assert outcome.rule_counts["VR012"] == 0


def test_catalogue_structure(rule_catalogue: list[Rule]) -> None:
    assert len(rule_catalogue) == 15
    assert [rule.id for rule in rule_catalogue] == [f"VR{number:03d}" for number in range(1, 16)]
    assert {rule.severity for rule in rule_catalogue} == {"error", "warn"}
    assert sum(1 for rule in rule_catalogue if rule.severity == "error") == 12
    assert sum(1 for rule in rule_catalogue if rule.severity == "warn") == 3
    assert all(rule.description for rule in rule_catalogue)


# --------------------------------------------------------------------------- #
# Catalogue loading failures (config errors -> CLI exit 2)
# --------------------------------------------------------------------------- #


def test_load_rules_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuleSpecError, match="not found"):
        load_rules(tmp_path / "missing.json")


def test_load_rules_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuleSpecError, match="not valid JSON"):
        load_rules(path)


def test_load_rules_duplicate_id(tmp_path: Path) -> None:
    rules = {
        "rules": [
            {"id": "R1", "description": "d", "field": "reading_id", "severity": "error",
             "predicate": {"type": "not_null"}},
            {"id": "R1", "description": "d", "field": "device_id", "severity": "error",
             "predicate": {"type": "not_null"}},
        ]
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(RuleSpecError, match="duplicate rule id"):
        load_rules(path)


def test_load_rules_unknown_predicate(tmp_path: Path) -> None:
    rules = {
        "rules": [
            {"id": "R1", "description": "d", "field": "reading_id", "severity": "error",
             "predicate": {"type": "magic"}},
        ]
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(RuleSpecError, match="unsupported predicate"):
        load_rules(path)


def test_load_rules_unknown_severity(tmp_path: Path) -> None:
    rules = {
        "rules": [
            {"id": "R1", "description": "d", "field": "reading_id", "severity": "fatal",
             "predicate": {"type": "not_null"}},
        ]
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(RuleSpecError, match="severity"):
        load_rules(path)


def test_validate_unknown_field_raises(
    make_row: Callable[..., dict[str, str]],
) -> None:
    rule = Rule(
        id="X001",
        description="unknown column",
        field="no_such_column",
        severity="error",
        predicate={"type": "not_null"},
    )
    row = make_row()
    frame = pd.DataFrame([row], columns=list(row.keys()))
    with pytest.raises(RuleSpecError, match="unknown field"):
        validate_frame(frame, [rule])


def test_validate_missing_context_source(
    make_row: Callable[..., dict[str, str]],
) -> None:
    rule = Rule(
        id="X002",
        description="needs context",
        field="device_id",
        severity="error",
        predicate={"type": "in_set", "source": "known_device_ids"},
    )
    row = make_row()
    frame = pd.DataFrame([row], columns=list(row.keys()))
    with pytest.raises(RuleSpecError, match="context source"):
        validate_frame(frame, [rule], context={})


# --------------------------------------------------------------------------- #
# Rejects CSV
# --------------------------------------------------------------------------- #


def test_write_rejects_csv(
    tmp_path: Path,
    make_row: Callable[..., dict[str, str]],
    rule_catalogue: list[Rule],
    known_device_ids: list[str],
) -> None:
    clean = make_row()
    broken = make_row(power_kw="-1.0")
    frame = pd.DataFrame([clean, broken], columns=list(clean.keys()))
    outcome = validate_frame(
        frame, rule_catalogue, context={"known_device_ids": known_device_ids}, now=NOW
    )

    target = tmp_path / "rejects.csv"
    written = write_rejects(frame, outcome, target)

    assert written == 1
    assert target.is_file()
    rejects = pd.read_csv(target, dtype=str, keep_default_na=False)
    assert len(rejects) == 1
    assert rejects.loc[0, "source_row"] == "1"
    assert rejects.loc[0, "error_rules"] == "VR012"
    assert rejects.loc[0, "power_kw"] == "-1.0"


def test_write_rejects_csv_empty_when_all_clean(
    tmp_path: Path,
    make_row: Callable[..., dict[str, str]],
    rule_catalogue: list[Rule],
    known_device_ids: list[str],
) -> None:
    row = make_row()
    frame = pd.DataFrame([row], columns=list(row.keys()))
    outcome = validate_frame(
        frame, rule_catalogue, context={"known_device_ids": known_device_ids}, now=NOW
    )
    target = tmp_path / "rejects.csv"
    written = write_rejects(frame, outcome, target)
    assert written == 0
    rejects = pd.read_csv(target, dtype=str, keep_default_na=False)
    assert len(rejects) == 0
    assert "error_rules" in rejects.columns
