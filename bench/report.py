#!/usr/bin/env python3
"""Deprecated: use `obench report` or `python3 -m obench.report`."""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

print(
    "note: bench/report.py is deprecated; prefer `obench report` "
    "(or `python3 -m obench.report`).",
    file=sys.stderr,
)

from obench.report import main

if __name__ == "__main__":
    raise SystemExit(main())
