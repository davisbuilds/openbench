#!/usr/bin/env python3
"""Deprecated: use `obench run` or `python3 -m obench.run`."""

from __future__ import annotations

import sys

print(
    "note: bench/run.py is deprecated; prefer `obench run` "
    "(or `python3 -m obench.run`).",
    file=sys.stderr,
)

from obench.run import main

if __name__ == "__main__":
    raise SystemExit(main())
