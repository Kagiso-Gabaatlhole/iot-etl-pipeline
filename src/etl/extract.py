"""Extract stage: load raw CSVs and fingerprint them.

The stage is intentionally small and side-effect free apart from file reads:
it returns the raw frames plus a :class:`FileMeta` record (path, SHA-256
checksum, row count) for every input file. The checksums flow into the load
manifest and the quality report so every warehouse run can be traced back to
the exact bytes it was built from.

All values are read as strings (``dtype=str``) so that malformed rows keep
their original content and can be judged by the validation rules rather than
by pandas' type inference.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

#: Canonical raw schema of the telemetry CSV (read in this order).
READINGS_COLUMNS: tuple[str, ...] = (
    "reading_id",
    "device_id",
    "timestamp",
    "temperature_c",
    "humidity_pct",
    "power_kw",
)

#: Canonical raw schema of the device registry CSV.
REGISTRY_COLUMNS: tuple[str, ...] = (
    "device_id",
    "device_type",
    "site",
    "firmware",
)

_CHUNK_BYTES = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class FileMeta:
    """Fingerprint of one input file.

    Attributes:
        path: Absolute path of the file.
        sha256: Lowercase hex SHA-256 of the file's bytes.
        row_count: Number of data rows (excluding the header).
    """

    path: Path
    sha256: str
    row_count: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable representation used in summaries/reports."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "row_count": self.row_count,
        }


@dataclass(frozen=True)
class ExtractResult:
    """Output of the extract stage.

    Attributes:
        readings: Raw telemetry frame (all values as strings).
        device_registry: Device registry frame (all values as strings).
        files: Metadata for both input files, readings first.
    """

    readings: pd.DataFrame
    device_registry: pd.DataFrame
    files: tuple[FileMeta, ...]

    @property
    def readings_meta(self) -> FileMeta:
        """Metadata of the telemetry CSV (first entry)."""
        return self.files[0]

    @property
    def rows_read(self) -> int:
        """Number of raw telemetry rows extracted."""
        return self.readings_meta.row_count


def sha256_of(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path`` (streamed, 1 MiB chunks)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: Path, expected_columns: tuple[str, ...], *, label: str) -> pd.DataFrame:
    if not Path(path).is_file():
        raise FileNotFoundError(f"{label} CSV not found: {path}")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    missing = [column for column in expected_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{label} CSV {path} is missing expected columns: {', '.join(missing)}")
    logger.debug(
        "extract.read file=%s label=%s rows=%d columns=%s",
        path, label, len(frame), list(frame.columns),
    )
    return frame


def extract(readings_csv: Path, device_registry_csv: Path) -> ExtractResult:
    """Load the raw CSVs and fingerprint them.

    Args:
        readings_csv: Path of the synthetic telemetry CSV.
        device_registry_csv: Path of the device registry CSV.

    Returns:
        :class:`ExtractResult` with both frames and both file fingerprints.

    Raises:
        FileNotFoundError: An input file does not exist.
        ValueError: An input file is missing expected columns.
    """
    readings_path = Path(readings_csv).resolve()
    registry_path = Path(device_registry_csv).resolve()

    readings = _read_csv(readings_path, READINGS_COLUMNS, label="readings")
    registry = _read_csv(registry_path, REGISTRY_COLUMNS, label="device_registry")

    files = (
        FileMeta(path=readings_path, sha256=sha256_of(readings_path), row_count=len(readings)),
        FileMeta(path=registry_path, sha256=sha256_of(registry_path), row_count=len(registry)),
    )
    logger.info(
        "extract.done readings_rows=%d registry_rows=%d readings_sha256=%s",
        files[0].row_count, files[1].row_count, files[0].sha256[:16] + "...",
    )
    return ExtractResult(readings=readings, device_registry=registry, files=files)
