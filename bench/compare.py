#!/usr/bin/env python3
"""Deprecated: use `obench compare` or `python3 -m obench.compare`."""

from __future__ import annotations

import sys

print(
    "note: bench/compare.py is deprecated; prefer `obench compare` "
    "(or `python3 -m obench.compare`).",
    file=sys.stderr,
)

from obench.compare import main

if __name__ == "__main__":
    raise SystemExit(main())
