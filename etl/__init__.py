"""Zero-install entry shim for ``python -m etl``.

The real package lives in ``src/etl/`` (a ``src/`` layout).  Rather than
requiring ``pip install -e .`` before the CLI can run from a fresh clone, this
tiny top-level package redirects ``etl.__path__`` at ``src/etl``.  Every
submodule (``etl.cli``, ``etl.extract``, ...) therefore loads from the real
source files, and ``python -m etl run`` works straight after ``git clone``.

The test suite imports ``etl`` the conventional way: the repository-root
``conftest.py`` puts ``src/`` on ``sys.path`` first, so tests exercise
``src/etl/__init__.py`` directly and this shim is never involved there.
"""

from __future__ import annotations

from pathlib import Path

#: Redirect submodule resolution to the real package under ``src/``.
__path__ = [str(Path(__file__).resolve().parent.parent / "src" / "etl")]

#: Mirrors ``src/etl/__init__.py`` (that file is bypassed when the shim loads).
__version__ = "1.0.0"
