#!/usr/bin/env python3
"""Fixture adapter that exposes the optional version() and solves write-marker.

Used to test that run.py stamps ``harness_version`` from an adapter's
``version()`` (vs None when an adapter omits it, e.g. fake_adapter).
"""

import os

NAME = "versioned_adapter"
MODELS = {"gpt-5.5-medium": "vfake/gpt-5.5-medium"}


def version():
    return "vfake-1.2.3"


def run(instruction, workdir, model, timeout_s):
    with open(os.path.join(workdir, "done.txt"), "w", encoding="utf-8") as fh:
        fh.write("SOLVED")
    return {
        "completed": True, "error": None, "output_tail": "wrote done.txt",
        "tokens": 100, "turns": 2, "cmd": ["versioned_adapter", "write"],
    }
