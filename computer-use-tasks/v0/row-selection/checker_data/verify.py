#!/usr/bin/env python3
import json
import pathlib
import re
import sys


EXPECTED = {"selected_row": "Row 003"}
SELECTED_ROW = re.compile(
    r'^\s*(e\d+@s\d+) AXRow\b[^\n]*\bselected\b'
    r'(?:(?!\n\s*e\d+@s\d+ AXRow\b).)*?\bRow 003\b',
    re.MULTILINE | re.DOTALL,
)


def load_object(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    value = json.loads(
        pathlib.Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda item: (_ for _ in ()).throw(
            ValueError("non-standard JSON constant: " + item)
        ),
    )
    if type(value) is not dict:
        raise ValueError(str(path) + " must contain one JSON object")
    return value


def final_answer(path):
    trajectory = load_object(path)
    messages = [
        step.get("message")
        for step in trajectory.get("steps", [])
        if type(step) is dict
        and step.get("source") == "agent"
        and type(step.get("message")) is str
        and step["message"].strip()
    ]
    if not messages:
        raise ValueError("ATIF has no final agent message")
    answer = json.loads(messages[-1].strip())
    if answer != EXPECTED:
        raise ValueError("final message must be the exact selected-row JSON object")


def selected_row_observed(path):
    saw_state = False
    for number, raw in enumerate(
        pathlib.Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw:
            raise ValueError(f"raw event line {number} is empty")
        event = json.loads(raw)
        item = event.get("item") if type(event) is dict else None
        if (
            event.get("type") != "item.completed"
            or type(item) is not dict
            or item.get("type") != "mcp_tool_call"
        ):
            continue
        if item.get("tool") == "get_app_state":
            saw_state = True
        result = item.get("result")
        content = result.get("content") if type(result) is dict else None
        if type(content) is not list:
            continue
        for part in content:
            if (
                type(part) is dict
                and part.get("type") == "text"
                and type(part.get("text")) is str
                and SELECTED_ROW.search(part["text"])
            ):
                if not saw_state:
                    raise ValueError("row selection occurred without an initial state read")
                return
    raise ValueError("no Computer Use OSS response proved Row 003 selected")


def write_result(path):
    output = pathlib.Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"schema_version": "openbench.row-selection-result.v1", **EXPECTED},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def main():
    try:
        events_path, trajectory_path, output_path = sys.argv[1:4]
        final_answer(trajectory_path)
        selected_row_observed(events_path)
        write_result(output_path)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        IndexError,
    ) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    print("PASS: Row 003 was observed selected in Computer Use OSS output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
