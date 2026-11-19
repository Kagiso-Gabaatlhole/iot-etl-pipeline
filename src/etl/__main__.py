"""Module entry point so the pipeline can be invoked as ``python -m etl``."""

from __future__ import annotations

import sys

from etl.cli import main

if __name__ == "__main__":
    sys.exit(main())
