#!/usr/bin/env python3
"""Deprecated: use `obench doctor` or `python3 -m obench.doctor`."""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

print(
    "note: bench/doctor.py is deprecated; prefer `obench doctor` "
    "(or `python3 -m obench.doctor`).",
    file=sys.stderr,
)

from obench.doctor import main

if __name__ == "__main__":
    raise SystemExit(main())
