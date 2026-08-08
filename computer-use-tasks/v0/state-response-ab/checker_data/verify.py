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


def load_object(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    return json.loads(
        pathlib.Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("non-standard JSON constant: " + value)
        ),
    )


def main():
    try:
        actual = load_object(sys.argv[1])
        if type(actual) is not dict:
            raise ValueError("state must be a JSON object")
        if actual != EXPECTED:
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
