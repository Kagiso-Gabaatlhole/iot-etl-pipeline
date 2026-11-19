"""Synthetic IoT telemetry data generator (SEED = 42).

**All data produced by this script is synthetic.**  It exists so the repository
is self-contained and the pipeline can be exercised with zero network access:

* 25 devices over a fixed 30-day window (2026-09-01 .. 2026-09-30),
* each reading carries temperature (C), humidity (%) and power (kW),
* exactly ``BASE_ROWS + plan.duplicates`` rows are written (default:
  118,200 base rows + 1,800 appended duplicates = 120,000 rows; 1,200 of the
  base rows are mutated in place, so 117,000 clean rows survive validation),
* quality problems are **intentionally injected** on purpose so the validate
  stage has something real to catch:

  - exact duplicate rows (warn-level, dropped by the transform stage),
  - missing values (empty cells),
  - out-of-range values (negative power, impossible temperatures/humidity),
  - impossible timestamps (dates in the future),
  - malformed rows (wrong types, e.g. ``temperature_c="abc"``),
  - unknown device ids (schema violations vs. the device registry).

Everything is driven by :data:`SEED`, so two invocations produce byte-identical
CSVs.  Run it directly::

    python src/pipeline/generate_data.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

#: Master seed for every random decision in this module.
SEED = 42

#: Number of clean base rows before injections. In-place mutations do not
#: change the row count; the 1,800 appended duplicates bring the file to
#: exactly 118_200 + 1_800 = 120,000 rows (of which 1,200 are mutated, so
#: 117,000 rows survive validation).
BASE_ROWS = 118_200

#: Fixed 30-day data window.
WINDOW_START = datetime(2026, 9, 1, 0, 0, 0)
WINDOW_END = datetime(2026, 9, 30, 23, 59, 59)

READINGS_HEADER: list[str] = [
    "reading_id",
    "device_id",
    "timestamp",
    "temperature_c",
    "humidity_pct",
    "power_kw",
]

REGISTRY_HEADER: list[str] = ["device_id", "device_type", "site", "firmware"]

DEVICE_TYPES: list[str] = [
    "thermostat",
    "smart_plug",
    "env_sensor",
    "hvac_controller",
    "lighting_controller",
]
SITES: list[str] = ["site-a", "site-b", "site-c", "site-d"]


@dataclass(frozen=True)
class InjectionPlan:
    """How many quality problems of each kind to inject.

    Mutations (missing/out-of-range/future/malformed/unknown device) rewrite
    existing rows in place; ``duplicates`` rows are appended copies of clean
    rows. The default plan matches the committed dataset: 1,200 mutated rows
    plus 1,800 appended duplicates.
    """

    duplicates: int = 1_800
    missing_values: int = 400
    out_of_range: int = 350
    future_timestamps: int = 200
    malformed: int = 150
    unknown_devices: int = 100

    @property
    def mutations(self) -> int:
        """Rows that are rewritten in place (i.e. excluded from duplication)."""
        return (
            self.missing_values
            + self.out_of_range
            + self.future_timestamps
            + self.malformed
            + self.unknown_devices
        )

    @property
    def total(self) -> int:
        """Total problem rows: duplicates (appended) + in-place mutations."""
        return self.mutations + self.duplicates


def build_devices(count: int = 25) -> list[dict[str, str]]:
    """Return the synthetic device registry rows."""
    devices: list[dict[str, str]] = []
    for index in range(1, count + 1):
        devices.append(
            {
                "device_id": f"DEV{index:03d}",
                "device_type": DEVICE_TYPES[(index - 1) % len(DEVICE_TYPES)],
                "site": SITES[(index - 1) % len(SITES)],
                "firmware": f"v{1 + index % 3}.{index % 10}",
            }
        )
    return devices


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_row(rng: random.Random, position: int) -> dict[str, str]:
    """Generate one plausible clean telemetry row."""
    span = int((WINDOW_END - WINDOW_START).total_seconds())
    timestamp = WINDOW_START + timedelta(seconds=rng.randrange(span + 1))
    device_number = rng.randint(1, 25)
    temperature = max(-10.0, min(48.0, rng.uniform(-8.0, 46.0) + rng.gauss(0.0, 1.5)))
    humidity = max(30.0, min(97.0, rng.uniform(30.0, 97.0)))
    power = rng.uniform(0.05, 12.0)
    return {
        "reading_id": f"R{position:07d}",
        "device_id": f"DEV{device_number:03d}",
        "timestamp": _iso(timestamp),
        "temperature_c": f"{temperature:.1f}",
        "humidity_pct": f"{humidity:.1f}",
        "power_kw": f"{power:.3f}",
    }


def _inject_missing(row: dict[str, str], variant: int) -> None:
    """Blank out one required cell."""
    field = ("humidity_pct", "power_kw", "timestamp")[variant % 3]
    row[field] = ""


def _inject_out_of_range(row: dict[str, str], variant: int, rng: random.Random) -> None:
    """Write an impossible measurement."""
    if variant % 3 == 0:
        row["power_kw"] = f"-{rng.uniform(1.0, 5.0):.3f}"  # negative power
    elif variant % 3 == 1:
        row["temperature_c"] = f"{rng.uniform(61.0, 99.0):.1f}"  # beyond 60 C
    else:
        row["humidity_pct"] = f"{rng.uniform(101.0, 150.0):.1f}"  # > 100 %


def _inject_future(row: dict[str, str], rng: random.Random) -> None:
    """Move the timestamp to an impossible (future) date."""
    moment = datetime(2031, 1, 15) + timedelta(seconds=rng.randrange(30 * 24 * 3600))
    row["timestamp"] = _iso(moment)


def _inject_malformed(row: dict[str, str], variant: int) -> None:
    """Break a cell's type/format so it cannot pass validation."""
    corruptions = (
        ("temperature_c", "abc"),
        ("timestamp", "not-a-date"),
        ("reading_id", ""),
        ("power_kw", "N/A"),
        ("humidity_pct", "warm"),
    )
    field, value = corruptions[variant % len(corruptions)]
    row[field] = value


def generate_readings(
    seed: int = SEED,
    base_rows: int = BASE_ROWS,
    plan: InjectionPlan | None = None,
) -> list[dict[str, str]]:
    """Generate the full raw telemetry dataset as a list of row dicts.

    Args:
        seed: RNG seed; identical seeds give identical datasets.
        base_rows: Number of clean rows before injections (must be at least
            ``plan.total - plan.duplicates`` so mutation targets exist).
        plan: Injection plan (defaults to :class:`InjectionPlan`).

    Returns:
        One dict per raw CSV row, in shuffled file order.
    """
    plan = plan or InjectionPlan()
    if base_rows < plan.mutations:
        raise ValueError(
            f"base_rows ({base_rows}) must be >= injected mutations ({plan.mutations})"
        )
    if base_rows < plan.duplicates:
        raise ValueError(f"base_rows ({base_rows}) must be >= duplicates ({plan.duplicates})")

    rng = random.Random(seed)
    rows = [_clean_row(rng, position + 1) for position in range(base_rows)]

    # Reserve disjoint index sets so duplicated rows stay clean and every
    # mutated row is unique.
    reserved = rng.sample(range(base_rows), plan.mutations + plan.duplicates)
    mutation_targets = reserved[: plan.mutations]
    duplicate_sources = reserved[plan.mutations :]

    kinds: list[str] = (
        ["missing"] * plan.missing_values
        + ["out_of_range"] * plan.out_of_range
        + ["future"] * plan.future_timestamps
        + ["malformed"] * plan.malformed
        + ["unknown_device"] * plan.unknown_devices
    )
    rng.shuffle(kinds)

    for variant, (index, kind) in enumerate(zip(mutation_targets, kinds)):
        row = rows[index]
        if kind == "missing":
            _inject_missing(row, variant)
        elif kind == "out_of_range":
            _inject_out_of_range(row, variant, rng)
        elif kind == "future":
            _inject_future(row, rng)
        elif kind == "malformed":
            _inject_malformed(row, variant)
        else:  # unknown_device
            row["device_id"] = "DEV999"

    rows.extend(dict(rows[index]) for index in duplicate_sources)
    rng.shuffle(rows)
    return rows


def write_readings_csv(rows: Sequence[dict[str, str]], path: Path) -> None:
    """Write telemetry rows to ``path`` (UTF-8, ``\n`` line endings)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=READINGS_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_registry_csv(devices: Sequence[dict[str, str]], path: Path) -> None:
    """Write the device registry to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REGISTRY_HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(devices)


def sha256_of(path: Path) -> str:
    """SHA-256 hex digest of ``path`` (used for the provenance note)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_dataset(
    output_dir: Path,
    seed: int = SEED,
    base_rows: int = BASE_ROWS,
    plan: InjectionPlan | None = None,
) -> dict[str, Any]:
    """Write both raw CSVs and return a summary of what was generated.

    Args:
        output_dir: Directory for ``iot_readings.csv`` / ``device_registry.csv``.
        seed: RNG seed.
        base_rows: Clean rows before injections.
        plan: Injection plan.

    Returns:
        Summary dict: paths, sha256 digests, row counts, injection counts.
    """
    plan = plan or InjectionPlan()
    output_dir = Path(output_dir)
    rows = generate_readings(seed=seed, base_rows=base_rows, plan=plan)
    devices = build_devices()

    readings_path = output_dir / "iot_readings.csv"
    registry_path = output_dir / "device_registry.csv"
    write_readings_csv(rows, readings_path)
    write_registry_csv(devices, registry_path)

    return {
        "seed": seed,
        "window_start": _iso(WINDOW_START),
        "window_end": _iso(WINDOW_END),
        "device_count": len(devices),
        "base_rows": base_rows,
        "injections": {
            "duplicates": plan.duplicates,
            "missing_values": plan.missing_values,
            "out_of_range": plan.out_of_range,
            "future_timestamps": plan.future_timestamps,
            "malformed": plan.malformed,
            "unknown_devices": plan.unknown_devices,
        },
        "readings": {
            "path": str(readings_path),
            "row_count": len(rows),
            "sha256": sha256_of(readings_path),
        },
        "device_registry": {
            "path": str(registry_path),
            "row_count": len(devices),
            "sha256": sha256_of(registry_path),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    """CLI parser for the generator script."""
    parser = argparse.ArgumentParser(
        description="Generate the synthetic raw IoT CSVs (seeded, deterministic)."
    )
    parser.add_argument(
        "--output-dir",
        default="data/raw",
        help="directory for the generated CSVs (default: %(default)s)",
    )
    parser.add_argument("--seed", type=int, default=SEED, help="RNG seed (default: %(default)s)")
    parser.add_argument(
        "--base-rows",
        type=int,
        default=BASE_ROWS,
        help="clean rows before injections (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the dataset and print a JSON summary."""
    args = build_parser().parse_args(argv)
    summary = generate_dataset(Path(args.output_dir), seed=args.seed, base_rows=args.base_rows)
    injections = summary["injections"]
    print("Synthetic dataset generated (all data is synthetic).")
    print(f"  seed                 : {summary['seed']}")
    print(f"  readings rows        : {summary['readings']['row_count']}")
    print(f"  readings sha256      : {summary['readings']['sha256']}")
    print(f"  readings path        : {summary['readings']['path']}")
    print(f"  registry rows        : {summary['device_registry']['row_count']}")
    print(f"  registry sha256      : {summary['device_registry']['sha256']}")
    print(
        "  injected problems    : "
        + ", ".join(f"{name}={count}" for name, count in injections.items())
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
