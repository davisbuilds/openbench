from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from obench.native_trial import (
    BUNDLE_SCHEMA_VERSION,
    LEDGER_SCHEMA_VERSION,
    NATIVE_SIDECAR_SCHEMA_VERSION,
    NativeTrialError,
    TASK_SIDECAR_SCHEMA_VERSION,
    _validate_state_response_mode,
    import_native_trial,
    load_native_trial,
)
from obench.mcp_stdio_collector import CallLedger
from obench.run import ROW_FIELDS


FIXTURE_CASES = (
    Path(__file__).parent / "fixtures" / "native_trial_cases.json"
)
HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


class StateResponseModeEvidenceTests(unittest.TestCase):
    def test_full_mode_requires_full_encoding_for_state_bearing_calls(self):
        contract = [{
            "tool": "click",
            "required_arguments": {"include_state": True},
        }]
        call = {
            "contract_sequence": 1,
            "computer_use_meta": {
                "metrics": {"perception": {"response_encoding": "diff"}}
            },
        }
        with self.assertRaisesRegex(
            NativeTrialError, "did not return full perception evidence"
        ):
            _validate_state_response_mode([call], contract, "full")

        call["computer_use_meta"]["metrics"]["perception"][
            "response_encoding"
        ] = "full"
        _validate_state_response_mode([call], contract, "full")

    def test_auto_mode_and_non_state_calls_do_not_require_full_encoding(self):
        diff_call = {
            "contract_sequence": 1,
            "computer_use_meta": {
                "metrics": {"perception": {"response_encoding": "diff"}}
            },
        }
        _validate_state_response_mode(
            [diff_call],
            [{"tool": "click", "required_arguments": {"include_state": True}}],
            "auto",
        )
        _validate_state_response_mode(
            [{"contract_sequence": 1, "computer_use_meta": {}}],
            [{"tool": "list_apps", "required_arguments": {}}],
            "full",
        )


def _canonical_bytes(value):
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_digest(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_ledger(
    root, prefix, trial_id, lock_sha256, records, *, timestamps=None
):
    ledger_path = root / prefix / "ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    previous_hash = "0" * 64
    lines = []
    for sequence, (kind, payload) in enumerate(records, 1):
        record = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "trial_id": trial_id,
            "lock_sha256": lock_sha256,
            "sequence": sequence,
            "kind": kind,
            "timestamp": (
                timestamps[sequence - 1]
                if timestamps is not None
                else f"2026-08-06T12:00:0{sequence}+00:00"
            ),
            "payload": payload,
            "previous_hash": previous_hash,
        }
        record["record_hash"] = _canonical_digest(record)
        previous_hash = record["record_hash"]
        lines.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    ledger_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    _write_json(
        root / prefix / "seal.json",
        {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "trial_id": trial_id,
            "lock_sha256": lock_sha256,
            "record_count": len(records),
            "last_sequence": len(records),
            "root_hash": previous_hash,
            "ledger_sha256": _sha256(ledger_path),
        },
    )


def _proxy_request_payload(
    *,
    request_sequence=1,
    request_unix_ns=1786017602000000000,
    response_unix_ns=1786017603000000000,
    duration_ms=1000.0,
    status=200,
    usage_available=True,
    input_tokens=100,
    cached_tokens=20,
    output_tokens=30,
):
    return {
        "request_sequence": request_sequence,
        "request_unix_ns": request_unix_ns,
        "response_unix_ns": response_unix_ns,
        "duration_ms": duration_ms,
        "paced_wait_ms": 0.0,
        "status": status,
        "model": "gpt-fixture",
        "usage_available": usage_available,
        "input_tokens": input_tokens if usage_available else None,
        "cached_tokens": cached_tokens if usage_available else None,
        "output_tokens": output_tokens if usage_available else None,
        "error_present": status >= 400,
    }


def _focus_payload(
    state,
    frontmost_bundle_id,
    *,
    attempt=1,
    frontmost_pid=123,
):
    return {
        "attempt": attempt,
        "state": state,
        "frontmost_bundle_id": frontmost_bundle_id,
        "frontmost_pid": frontmost_pid,
        "target_bundle_id": "com.openbench.fixture",
        "target_pid": 123,
    }


def _read_ledger(root, prefix):
    records = [
        json.loads(line)
        for line in (root / prefix / "ledger.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]
    return (
        [(record["kind"], record["payload"]) for record in records],
        [record["timestamp"] for record in records],
    )


def _write_mcp_ledger(
    root,
    trial_id,
    *,
    with_call=True,
    tool="set_value",
    delivery_tier="tier1-ax-attribute",
    include_delivery=True,
    include_failed_mutation=False,
    relay_failures=0,
):
    path = root / "mcp/ledger.jsonl"
    ledger = CallLedger(path, "native-cub-v0-run", trial_id)
    if include_failed_mutation:
        ledger.append_call({
            "tool": "click_menu_item",
            "status": "tool_error",
            "request_id_type": "str",
            "argument_digest": "sha256:" + HEX_C,
            "request_bytes": 90,
            "response_bytes": 70,
            "request_unix_ns": 1786017601000000000,
            "response_unix_ns": 1786017601100000000,
            "duration_ms": 100.0,
            "tool_is_error": True,
            "jsonrpc_error": {"present": False, "code": None},
            "computer_use_meta": {
                "delivery": None,
                "error": None,
                "focus": None,
                "outcome": None,
            },
            "process_returncode": None,
        })
    if with_call:
        computer_use_meta = {
            "error": None,
            "outcome": {
                "classification": "success",
                "failure_domain": None,
                "web_ax_echo_risk": None,
                "verification": {},
            },
            "focus": {"focus_changed": False},
        }
        if include_delivery:
            computer_use_meta["delivery"] = {
                "delivery_tier": delivery_tier,
                "fallback_reasons": [],
                "chain_rung": None,
            }
        ledger.append_call({
            "tool": tool,
            "status": "completed",
            "request_id_type": "str",
            "argument_digest": "sha256:" + HEX_B,
            "request_bytes": 100,
            "response_bytes": 80,
            "request_unix_ns": (
                1786017601200000000
                if include_failed_mutation
                else 1786017601000000000
            ),
            "response_unix_ns": 1786017602000000000,
            "duration_ms": 1000.0,
            "tool_is_error": False,
            "jsonrpc_error": {"present": False, "code": None},
            "computer_use_meta": computer_use_meta,
            "process_returncode": None,
        })
    ledger.seal(
        {
            "returncode": 0,
            "integrity_ok": relay_failures == 0,
            "malformed_frames": 0,
            "partial_frames": 0,
            "duplicate_request_ids": 0,
            "missing_responses": 0,
            "relay_failures": relay_failures,
        }
    )


def _reseal_manifest(root):
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.relative_to(root).as_posix() != "manifest.json":
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "trial_id": json.loads((root / "lock.json").read_text())["trial_id"],
            "lock_sha256": _sha256(root / "lock.json"),
            "result_sha256": _sha256(root / "result.json"),
            "files": files,
        },
    )


def _build_bundle(root, case, *, trial_id="native-cub-v0-trial1"):
    task_id = "computer-use-bench-v0-form-entry"
    final_path = "artifacts/final-state/state.json"
    final_value = {"invoice_id": "INV-1042", "saved": True}
    artifact_missing = case.get("artifact_missing", False)
    if not artifact_missing:
        _write_json(root / final_path, final_value)

    task_sidecar = {
        "schema_version": TASK_SIDECAR_SCHEMA_VERSION,
        "trial_id": trial_id,
        "task_id": task_id,
        "task_content_sha256": HEX_A,
        "instruction_sha256": HEX_B,
        "verifier_sha256": HEX_C,
    }
    _write_json(root / "task/task.json", task_sidecar)
    native_sidecar = {
        "schema_version": NATIVE_SIDECAR_SCHEMA_VERSION,
        "trial_id": trial_id,
        "task_id": task_id,
        "app_bundle_id": "com.openbench.fixture",
        "reset_contract_sha256": HEX_B,
        "success_contract_sha256": HEX_C,
        "final_state_allowlist": [final_path],
        "focus_policy": {
            "required_foreground_bundle_id": case.get(
                "required_foreground_bundle_id", "com.openbench.fixture"
            ),
            "forbidden_bundle_ids": case.get("forbidden_bundle_ids", []),
            "require_foreground_full_agent_phase": True,
            "forbid_global_delivery": True,
            "allowed_delivery_tiers": case.get(
                "allowed_delivery_tiers", ["tier1-ax-attribute"]
            ),
        },
        "mcp_policy": {
            "minimum_calls": case.get("minimum_mcp_calls", 1),
            "required_tool_categories": case.get(
                "required_tool_categories", ["mutation"]
            ),
        },
    }
    _write_json(root / "task/native.json", native_sidecar)

    lock = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "trial_id": trial_id,
        "created_at": "2026-08-06T11:59:59+00:00",
        "task": {
            "name": task_id,
            "sidecar_path": "task/task.json",
            "sidecar_sha256": _sha256(root / "task/task.json"),
        },
        "native_sidecar": {
            "path": "task/native.json",
            "sha256": _sha256(root / "task/native.json"),
        },
        "harness": {
            "name": "codex",
            "version": "0.200.0",
            "version_source": "native_cli",
        },
        "model": {
            "name": "gpt-fixture",
            "provider": "openai",
            "revision": "gpt-fixture-2026-08-01",
        },
        "mcp": {
            "name": "computer-use-mcp",
            "version": "0.9.0",
            "transport": "stdio",
            "server_sha256": HEX_A,
            "collector_run_id": "native-cub-v0-run",
        },
        "environment": {
            "platform": "macos",
            "os": {"version": "15.6", "build": "24G84"},
            "architecture": "arm64",
            "hardware_model": "MacFixture1,1",
            "app": {
                "bundle_id": "com.openbench.fixture",
                "version": "1.2.3",
                "build": "45",
                "code_signature_sha256": HEX_B,
            },
            "display": {
                "width_px": 1728,
                "height_px": 1117,
                "scale_factor": 2.0,
                "color_space": "Display P3",
            },
            "preflight": {
                "accessibility": True,
                "screen_recording": True,
                "app_installed": True,
                "display_stable": True,
                "focus_monitor_ready": case.get("preflight_ready", True),
            },
        },
        "budget": {"timeout_s": case["timeout_s"], "max_retries": 1},
        "evidence": {
            "proxy_required": case["proxy"],
            "process_monitor_required": True,
        },
    }
    _write_json(root / "lock.json", lock)
    lock_sha256 = _sha256(root / "lock.json")

    preflight_failed = case["status"] == "preflight_failed"
    trajectory_steps = [
        {
            "step_id": 1,
            "source": "user",
            "message": "Complete the native task.",
            "timestamp": "2026-08-06T12:00:00+00:00",
        }
    ]
    if not preflight_failed:
        trajectory_steps.append(
            {
                "step_id": 2,
                "source": "agent",
                "message": "Task attempt finished.",
                "model_name": "gpt-fixture",
                "timestamp": "2026-08-06T12:00:05+00:00",
                "metrics": {
                    "prompt_tokens": 100,
                    "cached_tokens": 20,
                    "completion_tokens": 30,
                },
            }
        )
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": trial_id,
        "agent": {
            "name": "codex",
            "version": "0.200.0",
            "model_name": "gpt-fixture",
        },
        "steps": trajectory_steps,
        "final_metrics": {
            "total_steps": len(trajectory_steps),
            "total_prompt_tokens": 0 if preflight_failed else 100,
            "total_cached_tokens": 0 if preflight_failed else 20,
            "total_completion_tokens": 0 if preflight_failed else 30,
        },
    }
    _write_json(root / "agent/trajectory.json", trajectory)

    artifact_entries = [
        {
            "path": final_path,
            "present": not artifact_missing,
            "sha256": (
                None if artifact_missing else _sha256(root / final_path)
            ),
            "size": (
                None
                if artifact_missing
                else (root / final_path).stat().st_size
            ),
            "media_type": "application/json",
            "classification": "public_evidence",
        }
    ]
    _write_json(
        root / "artifacts/manifest.json",
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "trial_id": trial_id,
            "lock_sha256": lock_sha256,
            "reviewed": True,
            "contains_sensitive_data": False,
            "artifacts": artifact_entries,
        },
    )
    final_state_sha256 = _canonical_digest(
        [
            {
                "path": final_path,
                "present": not artifact_missing,
                "sha256": artifact_entries[0]["sha256"],
                "size": artifact_entries[0]["size"],
            }
        ]
    )

    completed = case["status"] == "completed"
    error = (
        None
        if completed
        else "native preflight failed"
        if preflight_failed
        else "native trial exceeded its locked timeout"
    )
    failure_class = (
        "solved"
        if completed and case["checker_exit"] == 0
        else "wrong_answer"
        if completed
        else case["status"]
    )
    required_foreground = case.get(
        "required_foreground_bundle_id", "com.openbench.fixture"
    )
    foreground_pid = 123 if required_foreground == "com.openbench.fixture" else 456
    process_records = []
    process_timestamps = []
    focus_records = []
    focus_timestamps = []
    if not preflight_failed:
        attempt_count = case["retry_count"] + 1
        attempt_windows = {
            attempt: (
                (
                    "2026-08-06T12:00:00.100000+00:00",
                    "2026-08-06T12:00:00.200000+00:00",
                    "2026-08-06T12:00:00.500000+00:00",
                    "2026-08-06T12:00:00.600000+00:00",
                    [
                        "2026-08-06T12:00:00+00:00",
                        "2026-08-06T12:00:00.300000+00:00",
                        "2026-08-06T12:00:00.600000+00:00",
                    ],
                )
                if attempt < attempt_count
                else (
                    "2026-08-06T12:00:00.900000+00:00",
                    "2026-08-06T12:00:01+00:00",
                    "2026-08-06T12:00:08+00:00",
                    "2026-08-06T12:00:08.100000+00:00",
                    [
                        "2026-08-06T12:00:00.900000+00:00",
                        "2026-08-06T12:00:01.500000+00:00",
                        "2026-08-06T12:00:02.500000+00:00",
                        "2026-08-06T12:00:03.500000+00:00",
                        "2026-08-06T12:00:04.500000+00:00",
                        "2026-08-06T12:00:05.500000+00:00",
                        "2026-08-06T12:00:06.500000+00:00",
                        "2026-08-06T12:00:07.500000+00:00",
                        "2026-08-06T12:00:08.100000+00:00",
                    ],
                )
            )
            for attempt in range(1, attempt_count + 1)
        }
        for attempt, (
            setup_at,
            agent_start,
            agent_finish,
            terminal_at,
            _owner_times,
        ) in attempt_windows.items():
            for phase, timestamp in (
                ("setup", setup_at),
                ("terminal", terminal_at),
            ):
                for role, bundle_id, pid in (
                    ("target", "com.openbench.fixture", 123),
                    ("foreground", required_foreground, foreground_pid),
                ):
                    process_records.append((
                        "process_identity",
                        {
                            "attempt": attempt,
                            "phase": phase,
                            "role": role,
                            "bundle_id": bundle_id,
                            "pid": pid,
                            "version": "1.2.3",
                            "build": "45",
                            "binary_sha256": HEX_A,
                            "signature_sha256": HEX_B,
                            "cdhash": "c" * 40,
                            "process_start_token": (
                                "Fri Aug 7 12:00:00 2026"
                            ),
                        },
                    ))
                    process_timestamps.append(timestamp)
            for boundary, timestamp in (
                ("start", agent_start),
                ("finish", agent_finish),
            ):
                process_records.append((
                    "agent_boundary",
                    {"attempt": attempt, "boundary": boundary},
                ))
                process_timestamps.append(timestamp)
        for attempt, window in attempt_windows.items():
            for timestamp in window[4]:
                process_records.append((
                    "mcp_owner_sample",
                    {
                        "attempt": attempt,
                        "owned_serve_pid": 4321,
                        "unrelated_serve_pids": [],
                    },
                ))
                process_timestamps.append(timestamp)
        ordered_process = sorted(
            zip(process_timestamps, process_records), key=lambda item: item[0]
        )
        process_timestamps = [item[0] for item in ordered_process]
        process_records = [item[1] for item in ordered_process]
        for attempt, window in attempt_windows.items():
            sample_times = (
                window[4]
                if attempt < attempt_count
                else [
                    f"2026-08-06T12:00:{second:02d}+00:00"
                    for second in range(1, 9)
                ]
            )
            for timestamp in sample_times:
                focus_records.append((
                    "focus_sample",
                    _focus_payload(
                        "observed",
                        case.get(
                            "observed_foreground_bundle_id",
                            required_foreground,
                        ),
                        attempt=attempt,
                        frontmost_pid=foreground_pid,
                    ),
                ))
                focus_timestamps.append(timestamp)

    _write_json(
        root / "result.json",
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "trial_id": trial_id,
            "lock_sha256": lock_sha256,
            "status": case["status"],
            "attempts": case["retry_count"] + 1,
            "retry_count": case["retry_count"],
            "timeout_s": case["timeout_s"],
            "started_at": "2026-08-06T12:00:00+00:00",
            "finished_at": "2026-08-06T12:00:10+00:00",
            "agent_started_at": (
                None
                if preflight_failed
                else "2026-08-06T12:00:01+00:00"
            ),
            "agent_finished_at": (
                None
                if preflight_failed
                else "2026-08-06T12:00:08+00:00"
            ),
            "timings": {
                "env_setup_s": 10.0 if preflight_failed else 1.0,
                "agent_s": 0.0 if preflight_failed else 7.0,
                "verifier_s": 2.0 if completed else 0.0,
                "total_s": 10.0,
            },
            "outcome": {
                "completed": completed,
                "score": case["score"],
                "checker_exit": case["checker_exit"],
                "error": error,
                "failure_class": failure_class,
                "failure_reason": None if completed else "deadline_exceeded",
            },
            "mcp_event_count": (
                0
                if preflight_failed
                else 1 + int(case.get("include_failed_mutation", False))
            ),
            "focus_event_count": len(focus_records),
            "process_event_count": len(process_records),
        },
    )
    verifier_status = "judged" if completed else "not_run"
    _write_json(
        root / "verifier/reward.json",
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "trial_id": trial_id,
            "lock_sha256": lock_sha256,
            "status": verifier_status,
            "reward": case["score"],
        },
    )
    _write_json(
        root / "verifier/evidence.json",
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "trial_id": trial_id,
            "lock_sha256": lock_sha256,
            "status": verifier_status,
            "checker_exit": case["checker_exit"],
            "reward": case["score"],
            "task_content_sha256": HEX_A,
            "final_state_sha256": final_state_sha256,
        },
    )

    _write_mcp_ledger(
        root,
        trial_id,
        with_call=not preflight_failed,
        tool=case.get("mcp_tool", "set_value"),
        delivery_tier=case.get("delivery_tier", "tier1-ax-attribute"),
        include_delivery=case.get("include_delivery", True),
        include_failed_mutation=case.get("include_failed_mutation", False),
        relay_failures=case.get("mcp_relay_failures", 0),
    )
    _write_ledger(
        root,
        "focus",
        trial_id,
        lock_sha256,
        focus_records,
        timestamps=focus_timestamps or None,
    )
    _write_ledger(
        root,
        "process",
        trial_id,
        lock_sha256,
        process_records,
        timestamps=process_timestamps,
    )
    if case["proxy"]:
        _write_ledger(
            root,
            "proxy",
            trial_id,
            lock_sha256,
            [
                (
                    "model_request",
                    _proxy_request_payload(),
                ),
                (
                    "proxy_terminal",
                    {
                        "state": (
                            "SEALED"
                            if case["status"] == "completed"
                            else "ABORTED"
                        ),
                        "complete": case["status"] == "completed",
                        "incomplete_in_flight_count": (
                            0
                            if case["status"] == "completed"
                            else 1
                        ),
                        "source_record_count": 1,
                        "source_root_hash": HEX_A,
                        "source_ledger_sha256": HEX_B,
                    },
                ),
            ],
            timestamps=[
                "2026-08-06T12:00:03+00:00",
                "2026-08-06T12:00:04+00:00",
            ],
        )
    _reseal_manifest(root)


class NativeTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cases = json.loads(FIXTURE_CASES.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def bundle(self, name):
        path = self.root / name
        if path.exists():
            shutil.rmtree(path)
        _build_bundle(path, self.cases[name])
        return path

    def test_lock_validator_uses_collector_tool_catalog(self):
        from obench.native_trial import COMPUTER_USE_TOOLS

        self.assertIn("get_app_state", COMPUTER_USE_TOOLS)
        self.assertIn("type_text", COMPUTER_USE_TOOLS)

    def test_happy_fixture_normalizes_row_and_cross_bound_evidence(self):
        row = load_native_trial(self.bundle("happy"))

        self.assertEqual(set(row), set(ROW_FIELDS))
        self.assertEqual(row["exec_mode"], "native_macos")
        self.assertTrue(row["success"])
        self.assertTrue(row["completed"])
        self.assertEqual(row["score"], 1.0)
        self.assertEqual(row["tokens"], 110)
        self.assertEqual(row["tokens_proxy_calls"], 1)
        self.assertTrue(row["usage_ranking_eligible"])
        provenance = row["candidate_provenance"]
        self.assertEqual(provenance["kind"], "native_macos_trial")
        self.assertEqual(provenance["mcp_identity"]["version"], "0.9.0")
        self.assertEqual(provenance["environment_identity"]["platform"], "macos")
        self.assertEqual(provenance["focus_event_count"], 8)
        self.assertEqual(provenance["mcp_policy"]["source"], "task-native-sidecar")
        self.assertEqual(
            provenance["monitor_health_evidence"]["agent_phase_started_at"],
            "2026-08-06T12:00:01+00:00",
        )
        self.assertEqual(
            provenance["monitor_health_evidence"]["agent_phase_finished_at"],
            "2026-08-06T12:00:08+00:00",
        )
        self.assertIn(
            "not malicious operator forgery",
            provenance["operator_trust_boundary"],
        )

    def test_process_attribution_tampering_is_rejected(self):
        owner = self.bundle("happy")
        records, timestamps = _read_ledger(owner, "process")
        for kind, payload in records:
            if kind == "mcp_owner_sample":
                payload["unrelated_serve_pids"] = [4312]
                break
        _write_ledger(
            owner,
            "process",
            "native-cub-v0-trial1",
            _sha256(owner / "lock.json"),
            records,
            timestamps=timestamps,
        )
        _reseal_manifest(owner)
        with self.assertRaisesRegex(NativeTrialError, "serve owner observed"):
            load_native_trial(owner)

        swapped = self.bundle("happy")
        records, timestamps = _read_ledger(swapped, "process")
        for kind, payload in records:
            if (
                kind == "process_identity"
                and payload["phase"] == "terminal"
                and payload["role"] == "target"
            ):
                payload["pid"] = 124
                break
        _write_ledger(
            swapped,
            "process",
            "native-cub-v0-trial1",
            _sha256(swapped / "lock.json"),
            records,
            timestamps=timestamps,
        )
        _reseal_manifest(swapped)
        with self.assertRaisesRegex(NativeTrialError, "identity changed"):
            load_native_trial(swapped)

        reincarnated = self.bundle("happy")
        records, timestamps = _read_ledger(reincarnated, "process")
        for kind, payload in records:
            if (
                kind == "process_identity"
                and payload["phase"] == "terminal"
                and payload["role"] == "target"
            ):
                payload["process_start_token"] = (
                    "Fri Aug 7 12:00:01 2026"
                )
                break
        _write_ledger(
            reincarnated,
            "process",
            "native-cub-v0-trial1",
            _sha256(reincarnated / "lock.json"),
            records,
            timestamps=timestamps,
        )
        _reseal_manifest(reincarnated)
        with self.assertRaisesRegex(NativeTrialError, "identity changed"):
            load_native_trial(reincarnated)

        substituted = self.bundle("happy")
        records, timestamps = _read_ledger(substituted, "focus")
        records[0][1]["frontmost_pid"] = 999
        _write_ledger(
            substituted,
            "focus",
            "native-cub-v0-trial1",
            _sha256(substituted / "lock.json"),
            records,
            timestamps=timestamps,
        )
        _reseal_manifest(substituted)
        with self.assertRaisesRegex(NativeTrialError, "focus PID"):
            load_native_trial(substituted)

        gap = self.bundle("happy")
        records, timestamps = _read_ledger(gap, "process")
        retained = [
            (timestamp, record)
            for timestamp, record in zip(timestamps, records)
            if record[0] in {"process_identity", "agent_boundary"}
            or timestamp
            in {
                "2026-08-06T12:00:00.900000+00:00",
                "2026-08-06T12:00:08.100000+00:00",
            }
        ]
        _write_ledger(
            gap,
            "process",
            "native-cub-v0-trial1",
            _sha256(gap / "lock.json"),
            [record for _, record in retained],
            timestamps=[timestamp for timestamp, _ in retained],
        )
        result_path = gap / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["process_event_count"] = len(retained)
        _write_json(result_path, result)
        _reseal_manifest(gap)
        with self.assertRaisesRegex(NativeTrialError, "heartbeat gap"):
            load_native_trial(gap)

        removed = self.bundle("happy")
        records, timestamps = _read_ledger(removed, "process")
        retained = [
            (timestamp, record)
            for timestamp, record in zip(timestamps, records)
            if not (
                record[0] == "process_identity"
                and record[1]["phase"] == "terminal"
                and record[1]["role"] == "target"
            )
        ]
        _write_ledger(
            removed,
            "process",
            "native-cub-v0-trial1",
            _sha256(removed / "lock.json"),
            [record for _, record in retained],
            timestamps=[timestamp for timestamp, _ in retained],
        )
        result_path = removed / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["process_event_count"] = len(retained)
        _write_json(result_path, result)
        _reseal_manifest(removed)
        with self.assertRaisesRegex(
            NativeTrialError, "lacks setup/terminal target identity"
        ):
            load_native_trial(removed)

        reordered = self.bundle("happy")
        records, timestamps = _read_ledger(reordered, "process")
        rewritten = []
        for timestamp, record in zip(timestamps, records):
            if (
                record[0] == "process_identity"
                and record[1]["role"] == "target"
            ):
                timestamp = (
                    "2026-08-06T12:00:07+00:00"
                    if record[1]["phase"] == "setup"
                    else "2026-08-06T12:00:02+00:00"
                )
            rewritten.append((timestamp, record))
        rewritten.sort(key=lambda item: item[0])
        _write_ledger(
            reordered,
            "process",
            "native-cub-v0-trial1",
            _sha256(reordered / "lock.json"),
            [record for _, record in rewritten],
            timestamps=[timestamp for timestamp, _ in rewritten],
        )
        _reseal_manifest(reordered)
        with self.assertRaisesRegex(
            NativeTrialError, "terminal identity precedes setup"
        ):
            load_native_trial(reordered)

        early = self.bundle("happy")
        records, timestamps = _read_ledger(early, "process")
        rewritten = [
            (
                (
                    "2026-08-06T12:00:07+00:00"
                    if record[0] == "process_identity"
                    and record[1]["phase"] == "terminal"
                    else timestamp
                ),
                record,
            )
            for timestamp, record in zip(timestamps, records)
        ]
        rewritten.sort(key=lambda item: item[0])
        _write_ledger(
            early,
            "process",
            "native-cub-v0-trial1",
            _sha256(early / "lock.json"),
            [record for _, record in rewritten],
            timestamps=[timestamp for timestamp, _ in rewritten],
        )
        _reseal_manifest(early)
        with self.assertRaisesRegex(
            NativeTrialError, "do not cover agent boundaries"
        ):
            load_native_trial(early)

        missing_boundary = self.bundle("happy")
        records, timestamps = _read_ledger(missing_boundary, "process")
        retained = [
            (timestamp, record)
            for timestamp, record in zip(timestamps, records)
            if not (
                record[0] == "agent_boundary"
                and record[1]["boundary"] == "finish"
            )
        ]
        _write_ledger(
            missing_boundary,
            "process",
            "native-cub-v0-trial1",
            _sha256(missing_boundary / "lock.json"),
            [record for _, record in retained],
            timestamps=[timestamp for timestamp, _ in retained],
        )
        result_path = missing_boundary / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["process_event_count"] = len(retained)
        _write_json(result_path, result)
        _reseal_manifest(missing_boundary)
        with self.assertRaisesRegex(
            NativeTrialError, "lacks explicit agent boundaries"
        ):
            load_native_trial(missing_boundary)

        mismatched_boundary = self.bundle("happy")
        records, timestamps = _read_ledger(mismatched_boundary, "process")
        rewritten = [
            (
                (
                    "2026-08-06T12:00:07.900000+00:00"
                    if record[0] == "agent_boundary"
                    and record[1]["boundary"] == "finish"
                    else timestamp
                ),
                record,
            )
            for timestamp, record in zip(timestamps, records)
        ]
        rewritten.sort(key=lambda item: item[0])
        _write_ledger(
            mismatched_boundary,
            "process",
            "native-cub-v0-trial1",
            _sha256(mismatched_boundary / "lock.json"),
            [record for _, record in rewritten],
            timestamps=[timestamp for timestamp, _ in rewritten],
        )
        _reseal_manifest(mismatched_boundary)
        with self.assertRaisesRegex(
            NativeTrialError,
            "final agent boundary evidence does not match result",
        ):
            load_native_trial(mismatched_boundary)

    def test_terminal_fixture_preserves_timeout_and_retry_outcome(self):
        row = load_native_trial(self.bundle("terminal"))

        self.assertFalse(row["success"])
        self.assertFalse(row["completed"])
        self.assertIsNone(row["score"])
        self.assertIsNone(row["checker_exit"])
        self.assertEqual(row["failure_class"], "timeout")
        self.assertEqual(row["candidate_provenance"]["terminal_status"], "timeout")
        self.assertEqual(row["candidate_provenance"]["retry_count"], 1)
        self.assertFalse(row["candidate_provenance"]["proxy_measured"])

    def test_terminal_accepts_sealed_partial_mcp_but_completed_rejects_it(self):
        terminal_case = {
            **self.cases["terminal"],
            "mcp_relay_failures": 1,
        }
        terminal = self.root / "terminal-partial-mcp"
        _build_bundle(terminal, terminal_case)

        row = load_native_trial(terminal)

        self.assertFalse(row["candidate_provenance"]["mcp_integrity_ok"])
        self.assertEqual(
            row["candidate_provenance"]["mcp_terminal_summary"][
                "relay_failures"
            ],
            1,
        )

        completed_case = {
            **self.cases["happy"],
            "mcp_relay_failures": 1,
        }
        completed = self.root / "completed-partial-mcp"
        _build_bundle(completed, completed_case)

        with self.assertRaisesRegex(
            NativeTrialError,
            "collector terminal seal is not clean",
        ):
            load_native_trial(completed)

    def test_terminal_proxy_marks_incomplete_usage_as_non_ranking(self):
        row = load_native_trial(self.bundle("terminal_proxy"))

        self.assertEqual(row["failure_class"], "timeout")
        self.assertEqual(row["usage_evidence_grade"], "proxy_partial")
        self.assertFalse(row["usage_ranking_eligible"])
        self.assertEqual(
            row["usage_ranking_exclusion_reason"],
            "native_proxy_incomplete_request",
        )
        self.assertEqual(
            row["candidate_provenance"][
                "proxy_incomplete_in_flight_count"
            ],
            1,
        )
        self.assertEqual(row["candidate_provenance"]["retry_count"], 0)
        self.assertTrue(row["candidate_provenance"]["proxy_measured"])

    def test_preflight_fixture_accepts_clean_zero_event_focus_seal(self):
        row = load_native_trial(self.bundle("preflight"))

        self.assertEqual(row["failure_class"], "preflight_failed")
        self.assertEqual(row["turns"], 0)
        self.assertEqual(row["candidate_provenance"]["focus_event_count"], 0)
        self.assertEqual(row["candidate_provenance"]["mcp_event_count"], 0)

    def test_tampered_final_state_is_rejected(self):
        bundle = self.bundle("happy")
        (bundle / "artifacts/final-state/state.json").write_text(
            '{"saved":false}\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(NativeTrialError, "does not match manifest"):
            load_native_trial(bundle)

    def test_missing_final_state_is_evidence_for_wrong_answer_only(self):
        wrong_case = {
            **self.cases["happy"],
            "score": 0.0,
            "checker_exit": 1,
            "artifact_missing": True,
        }
        wrong = self.root / "wrong-missing-artifact"
        _build_bundle(wrong, wrong_case)

        row = load_native_trial(wrong)

        self.assertFalse(row["success"])
        self.assertEqual(row["failure_class"], "wrong_answer")
        self.assertEqual(
            row["candidate_provenance"]["missing_final_state_artifacts"],
            ["artifacts/final-state/state.json"],
        )

        solved_case = {
            **self.cases["happy"],
            "artifact_missing": True,
        }
        solved = self.root / "solved-missing-artifact"
        _build_bundle(solved, solved_case)
        with self.assertRaisesRegex(
            NativeTrialError,
            "successful trial has missing final-state artifacts",
        ):
            load_native_trial(solved)

    def test_focus_policy_rejects_target_activation_and_global_delivery(self):
        background = {
            **self.cases["happy"],
            "required_foreground_bundle_id": "org.openbench.FocusGuard",
            "forbidden_bundle_ids": ["com.openbench.fixture"],
            "observed_foreground_bundle_id": "com.openbench.fixture",
        }
        target_active = self.root / "target-active"
        _build_bundle(target_active, background)
        with self.assertRaisesRegex(NativeTrialError, "required focus policy|forbidden app"):
            load_native_trial(target_active)

        global_delivery = {
            **self.cases["happy"],
            "allowed_delivery_tiers": [
                "tier1-ax-attribute",
                "tier4-global-session-tap",
            ],
            "delivery_tier": "tier4-global-session-tap",
        }
        globally_delivered = self.root / "global-delivery"
        _build_bundle(globally_delivered, global_delivery)
        with self.assertRaisesRegex(NativeTrialError, "global delivery is forbidden"):
            load_native_trial(globally_delivered)

    def test_focus_density_covers_only_explicit_sealed_agent_phase(self):
        bundle = self.bundle("happy")
        lock_sha256 = _sha256(bundle / "lock.json")
        observed = _focus_payload("observed", "com.openbench.fixture")
        yielded = _focus_payload("yielded_to_human", "com.apple.finder")
        _write_ledger(
            bundle,
            "focus",
            "native-cub-v0-trial1",
            lock_sha256,
            [
                ("focus_yield", yielded),
                *[("focus_sample", observed) for _ in range(8)],
                ("focus_yield", yielded),
            ],
            timestamps=[
                "2026-08-06T12:00:00+00:00",
                *[
                    f"2026-08-06T12:00:{second:02d}+00:00"
                    for second in range(1, 9)
                ],
                "2026-08-06T12:00:09+00:00",
            ],
        )
        result_path = bundle / "result.json"
        result = json.loads(result_path.read_text())
        result["focus_event_count"] = 10
        _write_json(result_path, result)
        _reseal_manifest(bundle)

        row = load_native_trial(bundle)
        self.assertTrue(row["success"])

        result["attempts"] = 2
        result["retry_count"] = 1
        result["timings"]["env_setup_s"] = 0.0
        result["timings"]["agent_s"] = 8.0
        focus_records = [
            *[
                ("focus_sample", {**observed, "attempt": 1})
                for _ in range(3)
            ],
            ("focus_yield", {**yielded, "attempt": 2}),
            *[
                ("focus_sample", {**observed, "attempt": 2})
                for _ in range(8)
            ],
            ("focus_yield", {**yielded, "attempt": 2}),
        ]
        _write_ledger(
            bundle,
            "focus",
            "native-cub-v0-trial1",
            lock_sha256,
            focus_records,
            timestamps=[
                "2026-08-06T12:00:00.100000+00:00",
                "2026-08-06T12:00:00.300000+00:00",
                "2026-08-06T12:00:00.600000+00:00",
                "2026-08-06T12:00:00.900000+00:00",
                *[
                    f"2026-08-06T12:00:{second:02d}+00:00"
                    for second in range(1, 9)
                ],
                "2026-08-06T12:00:09+00:00",
            ],
        )
        result["focus_event_count"] = len(focus_records)
        process_records = []
        process_timestamps = []
        attempt_windows = {
            1: (
                "2026-08-06T12:00:00.100000+00:00",
                "2026-08-06T12:00:00.200000+00:00",
                "2026-08-06T12:00:00.500000+00:00",
                "2026-08-06T12:00:00.600000+00:00",
                (
                    "2026-08-06T12:00:00+00:00",
                    "2026-08-06T12:00:00.300000+00:00",
                    "2026-08-06T12:00:00.600000+00:00",
                ),
            ),
            2: (
                "2026-08-06T12:00:00.900000+00:00",
                "2026-08-06T12:00:01+00:00",
                "2026-08-06T12:00:08+00:00",
                "2026-08-06T12:00:08.100000+00:00",
                (
                    "2026-08-06T12:00:00.900000+00:00",
                    "2026-08-06T12:00:01.500000+00:00",
                    "2026-08-06T12:00:02.500000+00:00",
                    "2026-08-06T12:00:03.500000+00:00",
                    "2026-08-06T12:00:04.500000+00:00",
                    "2026-08-06T12:00:05.500000+00:00",
                    "2026-08-06T12:00:06.500000+00:00",
                    "2026-08-06T12:00:07.500000+00:00",
                    "2026-08-06T12:00:08.100000+00:00",
                ),
            ),
        }
        for attempt, (
            setup_at,
            agent_start,
            agent_finish,
            terminal_at,
            owner_times,
        ) in attempt_windows.items():
            for phase, timestamp in (
                ("setup", setup_at),
                ("terminal", terminal_at),
            ):
                for role in ("target", "foreground"):
                    process_records.append((
                        "process_identity",
                        {
                            "attempt": attempt,
                            "phase": phase,
                            "role": role,
                            "bundle_id": "com.openbench.fixture",
                            "pid": 123,
                            "version": "1.2.3",
                            "build": "45",
                            "binary_sha256": HEX_A,
                            "signature_sha256": HEX_B,
                            "cdhash": "c" * 40,
                            "process_start_token": (
                                "Fri Aug 7 12:00:00 2026"
                            ),
                        },
                    ))
                    process_timestamps.append(timestamp)
            for boundary, timestamp in (
                ("start", agent_start),
                ("finish", agent_finish),
            ):
                process_records.append((
                    "agent_boundary",
                    {"attempt": attempt, "boundary": boundary},
                ))
                process_timestamps.append(timestamp)
            for timestamp in owner_times:
                process_records.append((
                    "mcp_owner_sample",
                    {
                        "attempt": attempt,
                        "owned_serve_pid": 4321,
                        "unrelated_serve_pids": [],
                    },
                ))
                process_timestamps.append(timestamp)
        ordered = sorted(
            zip(process_timestamps, process_records), key=lambda item: item[0]
        )
        _write_ledger(
            bundle,
            "process",
            "native-cub-v0-trial1",
            lock_sha256,
            [item[1] for item in ordered],
            timestamps=[item[0] for item in ordered],
        )
        result["process_event_count"] = len(process_records)
        _write_json(result_path, result)
        _reseal_manifest(bundle)
        self.assertTrue(load_native_trial(bundle)["success"])

        result["timings"]["agent_s"] = 6.0
        _write_json(result_path, result)
        _reseal_manifest(bundle)
        with self.assertRaisesRegex(
            NativeTrialError,
            "does not contain the final measured agent phase",
        ):
            load_native_trial(bundle)

        result["attempts"] = 1
        result["retry_count"] = 0
        result["timings"]["env_setup_s"] = 1.0
        result["timings"]["agent_s"] = 7.0
        result["agent_finished_at"] = "2026-08-06T12:00:09+00:00"
        _write_json(result_path, result)
        _reseal_manifest(bundle)
        with self.assertRaisesRegex(
            NativeTrialError,
            "does not match explicit agent phase boundaries",
        ):
            load_native_trial(bundle)

    def test_retry_requires_focus_coverage_for_every_attempt(self):
        bundle = self.root / "completed-retry-focus"
        _build_bundle(
            bundle,
            {**self.cases["happy"], "retry_count": 1},
        )
        records, timestamps = _read_ledger(bundle, "focus")
        retained = [
            (timestamp, record)
            for timestamp, record in zip(timestamps, records)
            if record[1]["attempt"] != 1
        ]
        _write_ledger(
            bundle,
            "focus",
            "native-cub-v0-trial1",
            _sha256(bundle / "lock.json"),
            [record for _, record in retained],
            timestamps=[timestamp for timestamp, _ in retained],
        )
        result_path = bundle / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["focus_event_count"] = len(retained)
        _write_json(result_path, result)
        _reseal_manifest(bundle)

        with self.assertRaisesRegex(
            NativeTrialError,
            "attempt 1 focus/session health does not cover",
        ):
            load_native_trial(bundle)

    def test_delivery_tier_policy_distinguishes_observation_and_mutation_calls(self):
        for tool in ("list_apps", "get_app_state", "find"):
            with self.subTest(tool=tool):
                observation = {
                    **self.cases["happy"],
                    "mcp_tool": tool,
                    "include_delivery": False,
                    "required_tool_categories": ["observation"],
                }
                observation_bundle = self.root / f"{tool}-without-delivery"
                _build_bundle(observation_bundle, observation)
                self.assertTrue(load_native_trial(observation_bundle)["success"])

        mutation = {
            **self.cases["happy"],
            "include_delivery": False,
        }
        mutation_bundle = self.root / "mutation-without-delivery"
        _build_bundle(mutation_bundle, mutation)
        with self.assertRaisesRegex(
            NativeTrialError,
            "delivery tier is absent or forbidden",
        ):
            load_native_trial(mutation_bundle)

        forbidden_tier = {
            **self.cases["happy"],
            "delivery_tier": "tier2-menu-action",
        }
        forbidden_bundle = self.root / "mutation-forbidden-tier"
        _build_bundle(forbidden_bundle, forbidden_tier)
        with self.assertRaisesRegex(
            NativeTrialError,
            "delivery tier is absent or forbidden",
        ):
            load_native_trial(forbidden_bundle)

        failed_before_delivery = {
            **self.cases["happy"],
            "include_failed_mutation": True,
        }
        failed_before_delivery_bundle = self.root / "failed-before-delivery"
        _build_bundle(failed_before_delivery_bundle, failed_before_delivery)
        loaded = load_native_trial(failed_before_delivery_bundle)
        self.assertTrue(loaded["success"])

    def test_privacy_leak_is_rejected_even_after_manifest_is_resealed(self):
        bundle = self.bundle("happy")
        trajectory_path = bundle / "agent/trajectory.json"
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        trajectory["steps"][1]["message"] = "Contact operator@example.com"
        _write_json(trajectory_path, trajectory)
        _reseal_manifest(bundle)

        with self.assertRaisesRegex(NativeTrialError, "email address"):
            load_native_trial(bundle)

        embedded_path = self.bundle("happy")
        trajectory_path = embedded_path / "agent/trajectory.json"
        trajectory = json.loads(trajectory_path.read_text())
        trajectory["steps"][1]["message"] = "cwd=/Users/alice/private-project"
        _write_json(trajectory_path, trajectory)
        _reseal_manifest(embedded_path)
        with self.assertRaisesRegex(NativeTrialError, "absolute home/file path"):
            load_native_trial(embedded_path)

        oversized = self.bundle("happy")
        artifact_path = oversized / "artifacts/final-state/state.json"
        artifact_path.write_text("x" * (1024 * 1024) + " operator@example.com")
        artifact_manifest_path = oversized / "artifacts/manifest.json"
        artifact_manifest = json.loads(artifact_manifest_path.read_text())
        artifact_manifest["artifacts"][0]["sha256"] = _sha256(artifact_path)
        artifact_manifest["artifacts"][0]["size"] = artifact_path.stat().st_size
        _write_json(artifact_manifest_path, artifact_manifest)
        aggregate = [{
            "path": "artifacts/final-state/state.json",
            "present": True,
            "sha256": _sha256(artifact_path),
            "size": artifact_path.stat().st_size,
        }]
        verifier_path = oversized / "verifier/evidence.json"
        verifier = json.loads(verifier_path.read_text())
        verifier["final_state_sha256"] = _canonical_digest(aggregate)
        _write_json(verifier_path, verifier)
        _reseal_manifest(oversized)
        with self.assertRaisesRegex(NativeTrialError, "privacy scan limit"):
            load_native_trial(oversized)

    def test_proxy_and_focus_timelines_are_bound_to_mcp_activity(self):
        proxy = self.bundle("happy")
        lock_sha256 = _sha256(proxy / "lock.json")
        _write_ledger(
            proxy,
            "proxy",
            "native-cub-v0-trial1",
            lock_sha256,
            [
                (
                    "model_request",
                    _proxy_request_payload(
                        request_unix_ns=1786017540000000000,
                        response_unix_ns=1786017541000000000,
                    ),
                ),
                ("proxy_terminal", {
                    "state": "SEALED",
                    "complete": True,
                    "incomplete_in_flight_count": 0,
                    "source_record_count": 1,
                    "source_root_hash": HEX_A,
                    "source_ledger_sha256": HEX_B,
                }),
            ],
            timestamps=[
                "2026-08-06T11:59:00+00:00",
                "2026-08-06T12:00:05+00:00",
            ],
        )
        _reseal_manifest(proxy)
        with self.assertRaisesRegex(NativeTrialError, "outside trial timing"):
            load_native_trial(proxy)

    def test_proxy_counts_failed_request_without_inventing_usage(self):
        proxy = self.bundle("happy")
        lock_sha256 = _sha256(proxy / "lock.json")
        _write_ledger(
            proxy,
            "proxy",
            "native-cub-v0-trial1",
            lock_sha256,
            [
                (
                    "model_request",
                    _proxy_request_payload(
                        request_sequence=1,
                        request_unix_ns=1786017601000000000,
                        response_unix_ns=1786017601500000000,
                        duration_ms=500.0,
                        status=429,
                        usage_available=False,
                    ),
                ),
                (
                    "model_request",
                    _proxy_request_payload(
                        request_sequence=2,
                        request_unix_ns=1786017602000000000,
                        response_unix_ns=1786017603000000000,
                    ),
                ),
                (
                    "proxy_terminal",
                    {
                        "state": "SEALED",
                        "complete": True,
                        "incomplete_in_flight_count": 0,
                        "source_record_count": 2,
                        "source_root_hash": HEX_A,
                        "source_ledger_sha256": HEX_B,
                    },
                ),
            ],
            timestamps=[
                "2026-08-06T12:00:01.500000+00:00",
                "2026-08-06T12:00:03+00:00",
                "2026-08-06T12:00:04+00:00",
            ],
        )
        _reseal_manifest(proxy)

        row = load_native_trial(proxy)

        self.assertEqual(row["tokens_proxy_calls"], 2)
        self.assertEqual(row["tokens_proxy_input_uncached"], 80)
        self.assertEqual(row["tokens_proxy_output"], 30)

        yielded = self.bundle("happy")
        lock_sha256 = _sha256(yielded / "lock.json")
        _write_ledger(
            yielded,
            "focus",
            "native-cub-v0-trial1",
            lock_sha256,
            [
                ("focus_sample", _focus_payload(
                    "observed", "com.openbench.fixture"
                )),
                ("focus_yield", _focus_payload(
                    "yielded_to_human", "com.apple.finder"
                )),
            ],
            timestamps=[
                "2026-08-06T12:00:00+00:00",
                "2026-08-06T12:00:01.500000+00:00",
            ],
        )
        result_path = yielded / "result.json"
        result = json.loads(result_path.read_text())
        result["focus_event_count"] = 2
        _write_json(result_path, result)
        _reseal_manifest(yielded)
        with self.assertRaisesRegex(
            NativeTrialError,
            "unhealthy or unlocked-coverage gap|overlapped focus yielded",
        ):
            load_native_trial(yielded)

        concurrent = self.bundle("happy")
        mcp_path = concurrent / "mcp/ledger.jsonl"
        mcp_path.unlink()
        ledger = CallLedger(
            mcp_path,
            "native-cub-v0-run",
            "native-cub-v0-trial1",
        )
        base_call = {
            "tool": "set_value",
            "status": "completed",
            "request_id_type": "str",
            "argument_digest": "sha256:" + HEX_B,
            "request_bytes": 100,
            "response_bytes": 80,
            "duration_ms": 500.0,
            "tool_is_error": False,
            "jsonrpc_error": {"present": False, "code": None},
            "computer_use_meta": {
                "error": None,
                "outcome": None,
                "focus": None,
                "delivery": {
                    "delivery_tier": "tier1-ax-attribute",
                    "fallback_reasons": [],
                    "chain_rung": None,
                },
            },
            "process_returncode": None,
        }
        ledger.append_call({
            **base_call,
            "request_unix_ns": 1786017603000000000,
            "response_unix_ns": 1786017603500000000,
        })
        ledger.append_call({
            **base_call,
            "request_unix_ns": 1786017601000000000,
            "response_unix_ns": 1786017604000000000,
            "duration_ms": 3000.0,
        })
        ledger.seal({
            "returncode": 0,
            "integrity_ok": True,
            "malformed_frames": 0,
            "partial_frames": 0,
            "duplicate_request_ids": 0,
            "missing_responses": 0,
            "input_incomplete": False,
        })
        lock_sha256 = _sha256(concurrent / "lock.json")
        _write_ledger(
            concurrent,
            "focus",
            "native-cub-v0-trial1",
            lock_sha256,
            [
                ("focus_sample", _focus_payload(
                    "observed", "com.openbench.fixture"
                )),
                ("focus_yield", _focus_payload(
                    "yielded_to_human", "com.apple.finder"
                )),
                ("focus_sample", _focus_payload(
                    "observed", "com.openbench.fixture"
                )),
            ],
            timestamps=[
                "2026-08-06T12:00:00+00:00",
                "2026-08-06T12:00:02+00:00",
                "2026-08-06T12:00:02.500000+00:00",
            ],
        )
        result_path = concurrent / "result.json"
        result = json.loads(result_path.read_text())
        result["mcp_event_count"] = 2
        result["focus_event_count"] = 3
        _write_json(result_path, result)
        _reseal_manifest(concurrent)
        with self.assertRaisesRegex(
            NativeTrialError,
            "unhealthy or unlocked-coverage gap|overlapped focus yielded",
        ):
            load_native_trial(concurrent)

    def test_long_mcp_call_accepts_continuous_healthy_focus_heartbeats(self):
        bundle = self.bundle("happy")
        mcp_path = bundle / "mcp/ledger.jsonl"
        mcp_path.unlink()
        ledger = CallLedger(
            mcp_path,
            "native-cub-v0-run",
            "native-cub-v0-trial1",
        )
        ledger.append_call({
            "tool": "set_value",
            "status": "completed",
            "request_id_type": "str",
            "argument_digest": "sha256:" + HEX_B,
            "request_bytes": 100,
            "response_bytes": 80,
            "request_unix_ns": 1786017601000000000,
            "response_unix_ns": 1786017607000000000,
            "duration_ms": 6000.0,
            "tool_is_error": False,
            "jsonrpc_error": {"present": False, "code": None},
            "computer_use_meta": {
                "error": None,
                "outcome": {
                    "classification": "success",
                    "failure_domain": None,
                    "web_ax_echo_risk": None,
                    "verification": {},
                },
                "focus": {"focus_changed": False},
                "delivery": {
                    "delivery_tier": "tier1-ax-attribute",
                    "fallback_reasons": [],
                    "chain_rung": None,
                },
            },
            "process_returncode": None,
        })
        ledger.seal({
            "returncode": 0,
            "integrity_ok": True,
            "malformed_frames": 0,
            "partial_frames": 0,
            "duplicate_request_ids": 0,
            "missing_responses": 0,
            "input_incomplete": False,
        })
        _reseal_manifest(bundle)

        row = load_native_trial(bundle)

        self.assertTrue(row["success"])

    def test_completed_trial_requires_dense_monitor_and_declared_mcp_policy(self):
        sparse = self.bundle("happy")
        lock_sha256 = _sha256(sparse / "lock.json")
        payload = _focus_payload("observed", "com.openbench.fixture")
        _write_ledger(
            sparse,
            "focus",
            "native-cub-v0-trial1",
            lock_sha256,
            [("focus_sample", payload), ("focus_sample", payload)],
            timestamps=[
                "2026-08-06T12:00:01+00:00",
                "2026-08-06T12:00:08+00:00",
            ],
        )
        result_path = sparse / "result.json"
        result = json.loads(result_path.read_text())
        result["focus_event_count"] = 2
        _write_json(result_path, result)
        _reseal_manifest(sparse)
        with self.assertRaisesRegex(NativeTrialError, "heartbeat gap"):
            load_native_trial(sparse)

        no_calls = self.bundle("happy")
        (no_calls / "mcp/ledger.jsonl").unlink()
        _write_mcp_ledger(no_calls, "native-cub-v0-trial1", with_call=False)
        result_path = no_calls / "result.json"
        result = json.loads(result_path.read_text())
        result["mcp_event_count"] = 0
        _write_json(result_path, result)
        _reseal_manifest(no_calls)
        with self.assertRaisesRegex(NativeTrialError, "at least 1 MCP calls"):
            load_native_trial(no_calls)

        wrong_category = {
            **self.cases["happy"],
            "required_tool_categories": ["observation"],
        }
        category_bundle = self.root / "wrong-category"
        _build_bundle(category_bundle, wrong_category)
        with self.assertRaisesRegex(NativeTrialError, "missing required MCP tool categories"):
            load_native_trial(category_bundle)

    def test_public_artifacts_reject_mime_spoofing_and_binary_content(self):
        spoofed = self.bundle("happy")
        artifact_manifest_path = spoofed / "artifacts/manifest.json"
        artifact_manifest = json.loads(artifact_manifest_path.read_text())
        artifact_manifest["artifacts"][0]["media_type"] = "application/octet-stream"
        _write_json(artifact_manifest_path, artifact_manifest)
        _reseal_manifest(spoofed)
        with self.assertRaisesRegex(NativeTrialError, "media type is not publishable"):
            load_native_trial(spoofed)

        binary = self.bundle("happy")
        artifact_path = binary / "artifacts/final-state/state.json"
        artifact_path.write_bytes(b"text\x00operator@example.com")
        artifact_manifest_path = binary / "artifacts/manifest.json"
        artifact_manifest = json.loads(artifact_manifest_path.read_text())
        artifact_manifest["artifacts"][0]["media_type"] = "text/plain"
        artifact_manifest["artifacts"][0]["sha256"] = _sha256(artifact_path)
        artifact_manifest["artifacts"][0]["size"] = artifact_path.stat().st_size
        _write_json(artifact_manifest_path, artifact_manifest)
        aggregate = [{
            "path": "artifacts/final-state/state.json",
            "present": True,
            "sha256": _sha256(artifact_path),
            "size": artifact_path.stat().st_size,
        }]
        verifier_path = binary / "verifier/evidence.json"
        verifier = json.loads(verifier_path.read_text())
        verifier["final_state_sha256"] = _canonical_digest(aggregate)
        _write_json(verifier_path, verifier)
        _reseal_manifest(binary)
        with self.assertRaisesRegex(NativeTrialError, "binary control bytes"):
            load_native_trial(binary)

    def test_resealed_verdict_changes_terminal_result_identity(self):
        wrong_case = {
            **self.cases["happy"],
            "score": 0.0,
            "checker_exit": 1,
        }
        bundle = self.root / "resealed-verdict"
        _build_bundle(bundle, wrong_case)
        before = load_native_trial(bundle)

        result_path = bundle / "result.json"
        result = json.loads(result_path.read_text())
        result["outcome"].update(
            score=1.0,
            checker_exit=0,
            failure_class="solved",
        )
        _write_json(result_path, result)
        reward_path = bundle / "verifier/reward.json"
        reward = json.loads(reward_path.read_text())
        reward["reward"] = 1.0
        _write_json(reward_path, reward)
        evidence_path = bundle / "verifier/evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["checker_exit"] = 0
        evidence["reward"] = 1.0
        _write_json(evidence_path, evidence)
        _reseal_manifest(bundle)

        after = load_native_trial(bundle)
        self.assertEqual(
            before["candidate_provenance"]["lock_sha256"],
            after["candidate_provenance"]["lock_sha256"],
        )
        self.assertNotEqual(before["run_id"], after["run_id"])
        self.assertNotEqual(
            before["candidate_provenance"]["result_identity_sha256"],
            after["candidate_provenance"]["result_identity_sha256"],
        )

    def test_trial_index_and_verifier_types_are_unambiguous(self):
        ambiguous = self.root / "ambiguous"
        _build_bundle(
            ambiguous,
            self.cases["happy"],
            trial_id="native-cub-v0-attempt-a",
        )
        with self.assertRaisesRegex(NativeTrialError, "explicit positive trialN"):
            load_native_trial(ambiguous)

        boolean_verdict = self.bundle("happy")
        evidence_path = boolean_verdict / "verifier/evidence.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["checker_exit"] = False
        evidence["reward"] = True
        _write_json(evidence_path, evidence)
        reward_path = boolean_verdict / "verifier/reward.json"
        reward = json.loads(reward_path.read_text())
        reward["reward"] = True
        _write_json(reward_path, reward)
        _reseal_manifest(boolean_verdict)
        with self.assertRaisesRegex(NativeTrialError, "finite number"):
            load_native_trial(boolean_verdict)

    def test_partial_or_duplicate_evidence_is_rejected(self):
        bundle = self.bundle("happy")
        (bundle / "focus/seal.json").unlink()
        with self.assertRaisesRegex(NativeTrialError, "inventory mismatch"):
            load_native_trial(bundle)

        duplicate = self.bundle("terminal")
        result_path = duplicate / "result.json"
        text = result_path.read_text(encoding="utf-8")
        result_path.write_text(
            text.replace('"status": "timeout"', '"status": "timeout",\n  "status": "timeout"'),
            encoding="utf-8",
        )
        _reseal_manifest(duplicate)
        with self.assertRaisesRegex(NativeTrialError, "duplicate object key"):
            load_native_trial(duplicate)

        extra = self.bundle("happy")
        _write_json(extra / "artifacts/final-state/undeclared.json", {"saved": True})
        _reseal_manifest(extra)
        with self.assertRaisesRegex(NativeTrialError, "final-state file exactly"):
            load_native_trial(extra)

    def test_path_unsafe_manifest_and_synthetic_harbor_evidence_are_rejected(self):
        bundle = self.bundle("happy")
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({"path": "../escape", "sha256": HEX_A, "size": 0})
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(NativeTrialError, "safe relative POSIX path"):
            load_native_trial(bundle)

        synthetic = self.bundle("terminal")
        trajectory_path = synthetic / "agent/trajectory.json"
        trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        trajectory["extra"] = {"harbor_job": "fabricated"}
        _write_json(trajectory_path, trajectory)
        _reseal_manifest(synthetic)
        with self.assertRaisesRegex(NativeTrialError, "synthetic Harbor field"):
            load_native_trial(synthetic)

    def test_import_appends_once_and_rejects_duplicate_run_id(self):
        bundle = self.bundle("happy")
        results = self.root / "results.jsonl"
        row = import_native_trial(bundle, results)

        saved = json.loads(results.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_id"], row["run_id"])
        with self.assertRaisesRegex(NativeTrialError, "duplicate run_id"):
            import_native_trial(bundle, results)


if __name__ == "__main__":
    unittest.main()
