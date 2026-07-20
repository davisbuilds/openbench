#!/usr/bin/env python3
"""Deprecated: use `obench gate` or `python3 -m obench.candidate_gate`."""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

print(
    "note: bench/candidate_gate.py is deprecated; prefer `obench gate` "
    "(or `python3 -m obench.candidate_gate`).",
    file=sys.stderr,
)

from obench.candidate_gate import main

if __name__ == "__main__":
    raise SystemExit(main())
