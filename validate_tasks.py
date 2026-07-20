#!/usr/bin/env python3
"""Backward-compatible entry point for ``python3 validate_tasks.py``."""

from __future__ import annotations

import sys

from obench.validate_tasks import main

if __name__ == "__main__":
    sys.exit(main())
