#!/usr/bin/env python3
"""Deprecated: use `obench doctor` or `python3 -m obench.doctor`."""

from __future__ import annotations

import sys

print(
    "note: bench/doctor.py is deprecated; prefer `obench doctor` "
    "(or `python3 -m obench.doctor`).",
    file=sys.stderr,
)

from obench.doctor import main

if __name__ == "__main__":
    raise SystemExit(main())
