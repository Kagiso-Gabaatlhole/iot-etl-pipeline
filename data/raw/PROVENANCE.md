# Data provenance — `data/raw/`

**All files in this directory are synthetic.** No real devices, customers or
telemetry are represented anywhere in this repository.

| File | Rows | SHA-256 |
| --- | ---: | --- |
| `iot_readings.csv` | 120,000 | `6b11dbdd9d0e1604a7010f33772d243ab6df0e2e5f2580fa0d9a46a98a44110c` |
| `device_registry.csv` | 25 | `84ae45badb6a0fb99908fc6fdc97f7491cf577386b10a87eb4c7d2bf6fe44a62` |

* Generator: `src/pipeline/generate_data.py` (`SEED = 42`, deterministic —
  re-running it reproduces byte-identical files).
* Window: 2026-09-01T00:00:00 .. 2026-09-30T23:59:59 (30 days), 25 devices.
* Intentionally injected quality problems (so validation has something to
  catch): 1,800 exact duplicate rows (appended), and 1,200 rows mutated in
  place — 400 missing values, 350 out-of-range values, 200 future timestamps,
  150 malformed (wrong-type) rows, 100 unknown device ids.
* 117,000 rows survive validation and load into the warehouse.

Regenerate with:

```console
python src/pipeline/generate_data.py
```
