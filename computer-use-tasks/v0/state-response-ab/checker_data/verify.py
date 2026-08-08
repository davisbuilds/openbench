#!/usr/bin/env python3
import json
import pathlib
import sys


EXPECTED = {
    "fixture": "basic-controls",
    "honest_counter": 2,
    "keystroke_echo": "openbench-42",
    "schema_version": 1,
    "toggle_on": True,
}


def main():
    try:
        actual = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
        if type(actual) is not dict or actual != EXPECTED:
            raise ValueError("state does not exactly match the success contract")
        if type(actual["honest_counter"]) is not int:
            raise ValueError("honest_counter must be an integer")
        if type(actual["toggle_on"]) is not bool:
            raise ValueError("toggle_on must be a boolean")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, IndexError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    print("PASS: state-response A/B fixture state is exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
