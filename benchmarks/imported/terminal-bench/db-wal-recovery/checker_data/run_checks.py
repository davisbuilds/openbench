#!/usr/bin/env python3
"""Stdlib checker for db-wal-recovery."""
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

TASK_DIR = pathlib.Path(os.environ["TASK_DIR"])
CWD = pathlib.Path.cwd()


def fail(message):
    print(f"FAIL: {message}")
    return 1


def main():
    out_path = CWD / "recovered.json"
    if not out_path.exists():
        return fail("recovered.json does not exist")
    try:
        actual = json.loads(out_path.read_text())
    except Exception as exc:
        return fail(f"recovered.json is not valid JSON: {exc}")

    if not isinstance(actual, list):
        return fail("recovered.json must contain a JSON list")
    required = {"id", "name", "value"}
    for i, item in enumerate(actual):
        if not isinstance(item, dict):
            return fail(f"item {i} is not an object")
        if set(item) != required:
            return fail(f"item {i} must have exactly id, name, and value fields")
        if not isinstance(item["id"], int) or isinstance(item["id"], bool):
            return fail(f"item {i} id is not an integer")
        if not isinstance(item["name"], str):
            return fail(f"item {i} name is not a string")
        if not isinstance(item["value"], int) or isinstance(item["value"], bool):
            return fail(f"item {i} value is not an integer")

    ids = [item["id"] for item in actual]
    if ids != sorted(ids):
        return fail("rows are not sorted by id")
    if len(ids) != len(set(ids)):
        return fail("duplicate ids found")

    expected = json.loads((TASK_DIR / "checker_data" / "expected_rows.json").read_text())
    if actual != expected:
        return fail("recovered rows do not match the checker-owned oracle")

    submitted_wal = CWD / "main.db-wal"
    if not submitted_wal.exists():
        return fail("main.db-wal is missing; the repaired WAL file must be present")
    try:
        wal_header = submitted_wal.read_bytes()[:4]
    except Exception as exc:
        return fail(f"could not read main.db-wal: {exc}")
    if wal_header not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
        return fail("main.db-wal does not have a valid SQLite WAL header")

    # Prove the submitted WAL, not a reconstructed main.db, performs the
    # recovery. Combine it with a checker-owned copy of the original base DB and
    # ask SQLite to apply the WAL frames.
    try:
        with tempfile.TemporaryDirectory(prefix="wal-repair-check-") as tmpdir:
            tmp = pathlib.Path(tmpdir)
            shutil.copy2(TASK_DIR / "checker_data" / "main.db", tmp / "main.db")
            shutil.copy2(submitted_wal, tmp / "main.db-wal")
            conn = sqlite3.connect(str(tmp / "main.db"))
            rows = [
                {"id": row[0], "name": row[1], "value": row[2]}
                for row in conn.execute("SELECT id, name, value FROM items ORDER BY id")
            ]
            conn.close()
    except Exception as exc:
        return fail(f"SQLite could not apply the submitted WAL to the original base DB: {exc}")
    if rows != expected:
        return fail("submitted main.db-wal does not recover the expected rows from the original base DB")

    print("PASS: recovered.json and repaired main.db-wal match all recovered WAL rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
