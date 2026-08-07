#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys


STATE = {
    "button_status": "pressed",
    "fixture": "background-control",
    "menu_status": "menu",
    "schema_version": 1,
    "text_field": "openbench-background-42",
}
LEDGER_SCHEMA = "openbench.computer-use.focus-ledger.v1"
SEAL_SCHEMA = "openbench.computer-use.focus-seal.v1"
SOURCE = "NSWorkspace.frontmostApplication+computer-use-mcp.delivery"
ACTIONS = ("set_value", "click", "menu_item")
ALLOWED_TIERS = {"ax-set-value", "ax-press", "ax-menu-action"}


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def loads(text):
    return json.loads(
        text,
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("non-standard JSON constant: " + value)
        ),
    )


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_object(path):
    value = loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError(str(path) + " must contain one JSON object")
    return value


def verify_state(path):
    state = read_object(path)
    if state != STATE:
        raise ValueError("fixture state does not exactly match the success contract")
    if type(state["schema_version"]) is not int:
        raise ValueError("fixture schema_version must be an integer")


def verify_focus(ledger_path, seal_path):
    ledger_bytes = pathlib.Path(ledger_path).read_bytes()
    try:
        lines = ledger_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("focus ledger is not UTF-8") from exc
    if len(lines) != len(ACTIONS) or any(not line for line in lines):
        raise ValueError("focus ledger must contain exactly three non-empty records")

    records = [loads(line) for line in lines]
    previous = "0" * 64
    guard_bundle = None
    for index, (record, action) in enumerate(zip(records, ACTIONS), 1):
        expected_fields = {
            "kind", "payload", "previous_hash", "record_hash", "schema_version",
            "sequence", "source",
        }
        if type(record) is not dict or set(record) != expected_fields:
            raise ValueError("focus record has an inexact schema")
        if record["schema_version"] != LEDGER_SCHEMA:
            raise ValueError("unsupported focus ledger schema")
        if record["sequence"] != index or type(record["sequence"]) is not int:
            raise ValueError("focus ledger sequence is invalid")
        if record["kind"] != "action_observation" or record["source"] != SOURCE:
            raise ValueError("focus record source is not authoritative")
        if record["previous_hash"] != previous:
            raise ValueError("focus ledger hash chain is broken")
        unhashed = dict(record)
        claimed = unhashed.pop("record_hash")
        if claimed != digest(unhashed):
            raise ValueError("focus record hash is invalid")
        previous = claimed

        payload = record["payload"]
        fields = {
            "action", "delivery_tier", "frontmost_bundle_id", "guard_pid",
            "target_active", "target_pid",
        }
        if type(payload) is not dict or set(payload) != fields:
            raise ValueError("focus payload has an inexact schema")
        if payload["action"] != action:
            raise ValueError("focus ledger action order is invalid")
        if payload["delivery_tier"] not in ALLOWED_TIERS:
            raise ValueError("global or unsupported delivery escalation observed")
        if type(payload["target_active"]) is not bool or payload["target_active"]:
            raise ValueError("target activation observed")
        if type(payload["guard_pid"]) is not int or type(payload["target_pid"]) is not int:
            raise ValueError("focus process ids must be integers")
        if payload["guard_pid"] <= 0 or payload["target_pid"] <= 0:
            raise ValueError("focus process ids must be positive")
        if payload["guard_pid"] == payload["target_pid"]:
            raise ValueError("guard and target must be separate processes")
        frontmost = payload["frontmost_bundle_id"]
        if type(frontmost) is not str or not frontmost or frontmost == "unknown":
            raise ValueError("frontmost guard bundle id is missing")
        if guard_bundle is None:
            guard_bundle = frontmost
        elif guard_bundle != frontmost:
            raise ValueError("guard app did not remain foreground")

    seal = read_object(seal_path)
    expected_seal_fields = {
        "authority", "global_delivery_observed", "ledger_sha256", "producer",
        "record_count", "root_hash", "schema_version", "target_activation_observed",
    }
    if set(seal) != expected_seal_fields:
        raise ValueError("focus seal has an inexact schema")
    if seal["schema_version"] != SEAL_SCHEMA:
        raise ValueError("unsupported focus seal schema")
    if seal["producer"] != "openbench-native-runner" or seal["authority"] != "runner-read-only":
        raise ValueError("focus seal is not runner-owned")
    if seal["record_count"] != len(records) or type(seal["record_count"]) is not int:
        raise ValueError("focus seal count does not match ledger")
    if seal["root_hash"] != previous:
        raise ValueError("focus seal root hash does not match ledger")
    if seal["ledger_sha256"] != hashlib.sha256(ledger_bytes).hexdigest():
        raise ValueError("focus seal digest does not match ledger")
    if seal["target_activation_observed"] is not False:
        raise ValueError("focus seal reports target activation")
    if seal["global_delivery_observed"] is not False:
        raise ValueError("focus seal reports global delivery")


def main():
    try:
        verify_state(sys.argv[1])
        verify_focus(sys.argv[2], sys.argv[3])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, IndexError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        return 1
    print("PASS: background state and focus policy are exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
