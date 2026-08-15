#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import sys
import unicodedata


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ANSWER_FIELDS = {"apple_account_name", "wallpaper"}


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


def normalize(value):
    if type(value) is not str:
        raise ValueError("answers must be strings")
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized or len(normalized) > 200:
        raise ValueError("answers must contain 1 to 200 normalized characters")
    return normalized


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_read_only_state():
    before_path = os.environ.get("OPENBENCH_SYSTEM_SETTINGS_BEFORE_PATH")
    if not before_path:
        return
    before = load_object(before_path)
    if before.get("schema_version") != "openbench.system-settings-before.v1":
        raise ValueError("invalid System Settings before-state schema")
    files = before.get("files")
    if type(files) is not list or len(files) != 2:
        raise ValueError("System Settings before-state must contain two files")
    seen = set()
    for item in files:
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise ValueError("invalid System Settings before-state file entry")
        path = item["path"]
        expected = item["sha256"]
        if (
            type(path) is not str
            or not pathlib.Path(path).is_absolute()
            or path in seen
            or type(expected) is not str
            or not HASH_RE.fullmatch(expected)
        ):
            raise ValueError("invalid System Settings before-state file identity")
        seen.add(path)
        if file_digest(path) != expected:
            raise ValueError("System Settings state changed during read-only task")


def expected_hashes():
    fallback = load_object(pathlib.Path(__file__).with_name("expected-hashes.json"))
    result = {
        "apple_account_name_sha256": os.environ.get(
            "OPENBENCH_EXPECTED_APPLE_NAME_SHA256",
            fallback.get("apple_account_name_sha256"),
        ),
        "wallpaper_sha256": os.environ.get(
            "OPENBENCH_EXPECTED_WALLPAPER_SHA256", fallback.get("wallpaper_sha256")
        ),
    }
    if any(type(value) is not str or not HASH_RE.fullmatch(value) for value in result.values()):
        raise ValueError("expected answer hashes must be lowercase SHA-256 values")
    return result


def final_answer(trajectory_path):
    trajectory = load_object(trajectory_path)
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
    raw = messages[-1].strip()
    answer = json.loads(raw, object_pairs_hook=unique_object)
    if type(answer) is not dict or set(answer) != ANSWER_FIELDS:
        raise ValueError("final message must be the exact two-field JSON object")
    return {key: normalize(answer[key]) for key in sorted(ANSWER_FIELDS)}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def observed_state_text(events_path):
    observations = []
    for line_number, line in enumerate(
        pathlib.Path(events_path).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line:
            raise ValueError(f"raw event line {line_number} is empty")
        event = json.loads(line)
        item = event.get("item") if type(event) is dict else None
        if (
            event.get("type") != "item.completed"
            or type(item) is not dict
            or item.get("type") != "mcp_tool_call"
            or item.get("tool") != "get_app_state"
        ):
            continue
        result = item.get("result")
        content = result.get("content") if type(result) is dict else None
        if type(content) is not list:
            continue
        observations.extend(
            part["text"]
            for part in content
            if type(part) is dict
            and part.get("type") == "text"
            and type(part.get("text")) is str
        )
    if len(observations) < 2:
        raise ValueError("at least two completed get_app_state observations are required")
    return "\n".join(observations)


def write_result(path, answer, hashes):
    output = pathlib.Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "apple_account_name_sha256": hashes["apple_account_name_sha256"],
        "schema_version": "openbench.system-settings-discovery-result.v1",
        "wallpaper": answer["wallpaper"],
        "wallpaper_sha256": hashes["wallpaper_sha256"],
    }
    output.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def main():
    try:
        events_path, trajectory_path, output_path = sys.argv[1:4]
        answer = final_answer(trajectory_path)
        hashes = expected_hashes()
        observed = unicodedata.normalize("NFC", observed_state_text(events_path))
        for field in ANSWER_FIELDS:
            if answer[field] not in observed:
                raise ValueError(field + " was not observed in a get_app_state response")
        if digest(answer["apple_account_name"]) != hashes["apple_account_name_sha256"]:
            raise ValueError("Apple Account name does not match the local oracle")
        if digest(answer["wallpaper"]) != hashes["wallpaper_sha256"]:
            raise ValueError("wallpaper does not match the local oracle")
        verify_read_only_state()
        write_result(output_path, answer, hashes)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        IndexError,
    ) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    print("PASS: both System Settings values match observed UI and local hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
