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


def _write_mcp_ledger(
    root, trial_id, *, with_call=True, delivery_tier="tier1-ax-attribute"
):
    path = root / "mcp/ledger.jsonl"
    ledger = CallLedger(path, "native-cub-v0-run", trial_id)
    if with_call:
        ledger.append_call({
            "tool": "set_value",
            "status": "completed",
            "request_id_type": "str",
            "argument_digest": "sha256:" + HEX_B,
            "request_bytes": 100,
            "response_bytes": 80,
            "request_unix_ns": 1786017601000000000,
            "response_unix_ns": 1786017602000000000,
            "duration_ms": 1000.0,
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
                    "delivery_tier": delivery_tier,
                    "fallback_reasons": [],
                    "chain_rung": None,
                },
            },
            "process_returncode": None,
        })
    ledger.seal(
        {
            "returncode": 0,
            "integrity_ok": True,
            "malformed_frames": 0,
            "partial_frames": 0,
            "duplicate_request_ids": 0,
            "missing_responses": 0,
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
        "evidence": {"proxy_required": case["proxy"]},
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
            "sha256": _sha256(root / final_path),
            "size": (root / final_path).stat().st_size,
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
            "mcp_event_count": 0 if preflight_failed else 1,
            "focus_event_count": 0 if preflight_failed else 1,
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
        delivery_tier=case.get("delivery_tier", "tier1-ax-attribute"),
    )
    _write_ledger(
        root,
        "focus",
        trial_id,
        lock_sha256,
        [] if preflight_failed else [
            (
                "focus_sample",
                {
                    "state": "observed",
                    "frontmost_bundle_id": case.get(
                        "observed_foreground_bundle_id", "com.openbench.fixture"
                    ),
                    "target_bundle_id": "com.openbench.fixture",
                },
            )
        ],
    )
    if case["proxy"]:
        _write_ledger(
            root,
            "proxy",
            trial_id,
            lock_sha256,
            [
                (
                    "model_usage",
                    {
                        "input_tokens": 100,
                        "cached_tokens": 20,
                        "output_tokens": 30,
                    },
                )
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
        self.assertEqual(provenance["focus_event_count"], 1)

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
            [("model_usage", {
                "input_tokens": 100,
                "cached_tokens": 20,
                "output_tokens": 30,
            })],
            timestamps=["2026-08-06T11:59:00+00:00"],
        )
        _reseal_manifest(proxy)
        with self.assertRaisesRegex(NativeTrialError, "outside trial timing"):
            load_native_trial(proxy)

        yielded = self.bundle("happy")
        lock_sha256 = _sha256(yielded / "lock.json")
        _write_ledger(
            yielded,
            "focus",
            "native-cub-v0-trial1",
            lock_sha256,
            [
                ("focus_sample", {
                    "state": "observed",
                    "frontmost_bundle_id": "com.openbench.fixture",
                    "target_bundle_id": "com.openbench.fixture",
                }),
                ("focus_yield", {
                    "state": "yielded_to_human",
                    "frontmost_bundle_id": "com.apple.finder",
                    "target_bundle_id": "com.openbench.fixture",
                }),
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
        with self.assertRaisesRegex(NativeTrialError, "overlapped focus yielded"):
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
                ("focus_sample", {
                    "state": "observed",
                    "frontmost_bundle_id": "com.openbench.fixture",
                    "target_bundle_id": "com.openbench.fixture",
                }),
                ("focus_yield", {
                    "state": "yielded_to_human",
                    "frontmost_bundle_id": "com.apple.finder",
                    "target_bundle_id": "com.openbench.fixture",
                }),
                ("focus_sample", {
                    "state": "observed",
                    "frontmost_bundle_id": "com.openbench.fixture",
                    "target_bundle_id": "com.openbench.fixture",
                }),
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
        with self.assertRaisesRegex(NativeTrialError, "overlapped focus yielded"):
            load_native_trial(concurrent)

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
