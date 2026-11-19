"""Pytest bootstrap: make the ``src/`` layout importable without installation.

Imported automatically by pytest before the test modules, so ``import etl``
and ``import pipeline`` resolve to ``src/etl`` and ``src/pipeline``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
