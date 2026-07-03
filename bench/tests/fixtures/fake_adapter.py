#!/usr/bin/env python3
"""Fixture adapter that actually solves the write-marker fixture task.

It writes ``done.txt`` containing ``SOLVED`` into ``workdir`` so the full
run.py pipeline yields ``success=true`` for this adapter (versus the built-in
null adapter, which yields ``success=false``). Conforms to ADAPTER_SPEC.md.
"""

import os

NAME = "fake_adapter"
MODELS = {"gpt-5.5-medium": "fake/gpt-5.5-medium"}


def run(instruction, workdir, model, timeout_s):
    """Solve the fixture task by writing the expected marker file."""
    target = os.path.join(workdir, "done.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("SOLVED")
    return {
        "completed": True,
        "error": None,
        "output_tail": "wrote done.txt",
        # Optional full transcript (ADAPTER_SPEC v1); the runner persists this in
        # preference to output_tail. Distinct sentinel so a test can tell which
        # field the runner wrote.
        "full_output": "FULL_TRANSCRIPT_BEGIN\nwrote done.txt\nFULL_TRANSCRIPT_END",
        "tokens": 42,
        "turns": 1,
        "cmd": ["fake_adapter", "write", "done.txt"],
    }
