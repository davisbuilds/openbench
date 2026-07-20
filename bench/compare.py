#!/usr/bin/env python3
"""Deprecated: use `obench compare` or `python3 -m obench.compare`."""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

print(
    "note: bench/compare.py is deprecated; prefer `obench compare` "
    "(or `python3 -m obench.compare`).",
    file=sys.stderr,
)

from obench.compare import main

if __name__ == "__main__":
    raise SystemExit(main())
