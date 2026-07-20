#!/usr/bin/env python3
"""Deprecated: use `obench run` or `python3 -m obench.run`."""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

print(
    "note: bench/run.py is deprecated; prefer `obench run` "
    "(or `python3 -m obench.run`).",
    file=sys.stderr,
)

from obench.run import main

if __name__ == "__main__":
    raise SystemExit(main())
