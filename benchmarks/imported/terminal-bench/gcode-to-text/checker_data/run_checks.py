#!/usr/bin/env python3
"""Stdlib checker for gcode-to-text."""
import hashlib
import json
import os
import pathlib
import sys

TASK_DIR = pathlib.Path(os.environ["TASK_DIR"])
DATA = TASK_DIR / "checker_data"
CWD = pathlib.Path.cwd()


def fail(message):
    print(f"FAIL: {message}")
    return 1


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    gcode = CWD / "text.gcode"
    if not gcode.is_file():
        return fail("text.gcode is missing")
    expected_hashes = json.loads((DATA / "input_hashes.json").read_text())
    if sha256(gcode) != expected_hashes["text.gcode"]:
        return fail("text.gcode hash does not match the checker-owned input artifact")
    if sha256(DATA / "text.gcode") != expected_hashes["text.gcode"]:
        return fail("checker-owned text.gcode hash mismatch")

    out = CWD / "out.txt"
    if not out.exists():
        return fail("out.txt does not exist")
    actual = out.read_text(errors="replace").strip()
    expected = (DATA / "expected_flag.txt").read_text().strip()
    if actual != expected:
        return fail("out.txt does not contain the decoded printed text")

    print("PASS: out.txt matches checker-owned decoded text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
