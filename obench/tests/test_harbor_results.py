#!/usr/bin/env python3
"""Golden Harbor 0.20.0 job-result ingestion tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from obench.harbor_results import (
    HARBOR_GIT_COMMIT,
    HARBOR_VERSION,
    HarborResultsError,
    _tree_digest,
    import_results,
    load_rows,
)
from obench.run import ROW_FIELDS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": str(_REPO_ROOT)
    + os.pathsep
    + os.environ.get("PYTHONPATH", ""),
}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _trial_lock(task: str, model: str = "model-x") -> dict:
    return {
        "schema_version": 2,
        "task": {
            "name": f"openbench/{task}",
            "type": "local",
            "digest": "sha256:" + ("a" if task == "alpha" else "b") * 64,
            "path": f"/tmp/tasks/{task}",
        },
        "install_only": False,
        "timeout_multiplier": 1.0,
        "agent": {
            "name": "codex",
            "model_name": model,
            "skills": [],
            "resume_trajectory": False,
            "extra_allowed_hosts": [],
            "kwargs": {},
            "mcp_servers": [],
        },
        "skills": [],
        "environment": {
            "type": "docker",
            "force_build": False,
            "delete": True,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "extra_docker_compose": [],
            "kwargs": {},
            "extra_allowed_hosts": [],
        },
        "verifier": {"disable": False, "environment_mode": "shared"},
    }


def _trial_result(
    name: str,
    task: str,
    trial_id: str,
    score: float,
    *,
    model: str = "model-x",
    offset_minutes: int = 0,
) -> dict:
    minute = offset_minutes
    return {
        "id": trial_id,
        "task_name": f"openbench/{task}",
        "trial_name": name,
        "trial_uri": f"file:///tmp/job/{name}",
        "task_id": {"path": f"/tmp/tasks/{task}"},
        "source": "openbench",
        "task_checksum": ("a" if task == "alpha" else "b") * 64,
        "config": {
            "task": {"path": f"/tmp/tasks/{task}"},
            "trial_name": name,
            "trials_dir": "/tmp/job",
            "job_id": "10000000-0000-0000-0000-000000000000",
            "agent": {"name": "codex", "model_name": model, "kwargs": {}},
            "environment": {"type": "docker", "kwargs": {}},
            "verifier": {"disable": False, "kwargs": {}},
            "artifacts": [
                {"source": "/app", "destination": "workspace"}
            ],
            "extra_instruction_paths": [],
        },
        "agent_info": {
            "name": "codex",
            "version": "1.2.3",
            "model_info": {"name": model, "provider": None},
        },
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 25,
            "n_output_tokens": 40,
            "cost_usd": 0.5,
        },
        "verifier_result": {"rewards": {"reward": score}},
        "verifier_environment_mode": "shared",
        "exception_info": None,
        "started_at": f"2026-07-20T10:{minute:02d}:00+00:00",
        "finished_at": f"2026-07-20T10:{minute + 4:02d}:00+00:00",
        "environment_setup": {
            "started_at": f"2026-07-20T10:{minute:02d}:00+00:00",
            "finished_at": f"2026-07-20T10:{minute:02d}:30+00:00",
        },
        "agent_setup": {
            "started_at": f"2026-07-20T10:{minute:02d}:30+00:00",
            "finished_at": f"2026-07-20T10:{minute + 1:02d}:00+00:00",
        },
        "agent_execution": {
            "started_at": f"2026-07-20T10:{minute + 1:02d}:00+00:00",
            "finished_at": f"2026-07-20T10:{minute + 3:02d}:00+00:00",
        },
        "verifier": {
            "started_at": f"2026-07-20T10:{minute + 3:02d}:00+00:00",
            "finished_at": f"2026-07-20T10:{minute + 4:02d}:00+00:00",
        },
        "step_results": None,
    }


def _trajectory(model: str = "model-x") -> dict:
    return {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "codex", "version": "1.2.3"},
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": "Complete the task.",
                "timestamp": "2026-07-20T10:01:00+00:00",
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "Done.",
                "timestamp": "2026-07-20T10:03:00+00:00",
                "model_name": model,
                "metrics": {
                    "prompt_tokens": 100,
                    "cached_tokens": 25,
                    "completion_tokens": 40,
                    "cost_usd": 0.5,
                },
            },
        ],
        "final_metrics": {
            "total_steps": 2,
            "total_prompt_tokens": 100,
            "total_cached_tokens": 25,
            "total_completion_tokens": 40,
            "total_cost_usd": 0.5,
        },
    }


class GoldenHarborJob:
    """Synthetic fixture shaped from Harbor v0.20.0 model serialization."""

    def __init__(self, root: Path, specs: list[dict] | None = None):
        self.root = root
        self.specs = specs or [
            {
                "name": "alpha__zeta",
                "task": "alpha",
                "id": "00000000-0000-0000-0000-000000000002",
                "score": 0.0,
                "offset": 5,
            },
            {
                "name": "alpha__able",
                "task": "alpha",
                "id": "00000000-0000-0000-0000-000000000001",
                "score": 1.0,
                "offset": 0,
            },
            {
                "name": "beta__only",
                "task": "beta",
                "id": "00000000-0000-0000-0000-000000000003",
                "score": 0.5,
                "offset": 10,
            },
        ]
        self.write()

    def write(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        locks = [_trial_lock(spec["task"]) for spec in self.specs]
        _write_json(
            self.root / "lock.json",
            {
                "schema_version": 3,
                "created_at": "2026-07-20T09:59:00+00:00",
                "harbor": {
                    "version": HARBOR_VERSION,
                    "git_commit_hash": HARBOR_GIT_COMMIT,
                    "is_editable": False,
                },
                "n_concurrent_trials": 2,
                "retry": {
                    "max_retries": 0,
                    "include_exceptions": None,
                    "exclude_exceptions": None,
                },
                "trials": locks,
            },
        )
        results = []
        for spec, lock in zip(self.specs, locks):
            trial_dir = self.root / spec["name"]
            result = _trial_result(
                spec["name"],
                spec["task"],
                spec["id"],
                spec["score"],
                offset_minutes=spec["offset"],
            )
            _write_json(trial_dir / "lock.json", lock)
            _write_json(trial_dir / "result.json", result)
            _write_json(trial_dir / "agent" / "trajectory.json", _trajectory())
            (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
            (trial_dir / "verifier" / "reward.txt").write_text(
                f"{spec['score']}\n"
            )
            _write_json(
                trial_dir
                / "verifier"
                / "openbench-verifier-evidence.json",
                {
                    "schema_version": "openbench-verifier-evidence-v1",
                    "checker_exit": 0 if spec["score"] == 1.0 else 1,
                    "parsed_score": None if spec["score"] == 0.0 else spec["score"],
                    "reward": spec["score"],
                    "verifier_duration_seconds": 60,
                },
            )
            _write_json(
                trial_dir / "artifacts" / "manifest.json",
                [
                    {
                        "source": "/logs/artifacts",
                        "destination": "artifacts/logs/artifacts",
                        "type": "directory",
                        "status": "empty",
                        "service": None,
                    },
                    {
                        "source": "/app",
                        "destination": "artifacts/workspace",
                        "type": "directory",
                        "status": "ok",
                        "service": None,
                    },
                ],
            )
            workspace = trial_dir / "artifacts" / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "answer.txt").write_text(f"{spec['name']}\n")
            results.append(result)
        _write_json(
            self.root / "result.json",
            {
                "id": "10000000-0000-0000-0000-000000000000",
                "started_at": "2026-07-20T09:59:00+00:00",
                "updated_at": "2026-07-20T10:14:00+00:00",
                "finished_at": "2026-07-20T10:15:00+00:00",
                "n_total_trials": len(results),
                "stats": self._stats(results),
            },
        )

    def trial(self, index: int = 0) -> Path:
        return self.root / self.specs[index]["name"]

    def sync_aggregate(self) -> None:
        job_result_path = self.root / "result.json"
        job_result = json.loads(job_result_path.read_text())
        results = [
            json.loads((self.trial(index) / "result.json").read_text())
            for index in range(len(self.specs))
        ]
        job_result["stats"] = self._stats(results)
        _write_json(job_result_path, job_result)

    @staticmethod
    def _stats(results: list[dict]) -> dict:
        reward_stats: dict[str, list[str]] = {}
        for result in results:
            score = result["verifier_result"]["rewards"]["reward"]
            reward_stats.setdefault(str(score), []).append(result["trial_name"])
        return {
            "n_completed_trials": len(results),
            "n_errored_trials": 0,
            "n_running_trials": 0,
            "n_pending_trials": 0,
            "n_cancelled_trials": 0,
            "n_retries": 0,
            "evals": {
                "codex__model-x__openbench": {
                    "n_trials": len(results),
                    "n_errors": 0,
                    "metrics": [],
                    "pass_at_k": {},
                    "reward_stats": {"reward": reward_stats},
                    "exception_stats": {},
                }
            },
            "n_input_tokens": 100 * len(results),
            "n_cache_tokens": 25 * len(results),
            "n_output_tokens": 40 * len(results),
            "cost_usd": 0.5 * len(results),
        }


class HarborResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self) -> GoldenHarborJob:
        return GoldenHarborJob(self.root / "job")

    def test_golden_job_maps_groups_deterministically_without_temporal_claim(self):
        fixture = self.fixture()
        rows = load_rows(fixture.root)

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [(row["task"], row["trial"]) for row in rows],
            [("alpha", 1), ("alpha", 2), ("beta", 1)],
        )
        self.assertEqual(
            rows[0]["candidate_provenance"]["harbor_trial_name"], "alpha__able"
        )
        self.assertEqual(
            rows[1]["candidate_provenance"]["harbor_trial_name"], "alpha__zeta"
        )
        self.assertFalse(
            rows[0]["candidate_provenance"]["temporal_matched_block_claim"]
        )
        self.assertEqual(rows[0]["exec_mode"], "harbor")
        self.assertEqual(rows[0]["token_basis"], "harbor_agent_reported")
        self.assertEqual(rows[0]["tokens_input_uncached"], 75)
        self.assertEqual(rows[0]["tokens_cache_read"], 25)
        self.assertEqual(rows[0]["tokens_output"], 40)
        self.assertEqual(rows[0]["tokens"], 115)
        self.assertIsNone(rows[0]["token_basis_proxy"])
        self.assertIsNone(rows[0]["tokens_proxy_calls"])
        self.assertFalse(rows[0]["candidate_provenance"]["proxy_measured"])
        self.assertEqual(rows[0]["failure_class"], "solved")
        self.assertEqual(rows[1]["failure_class"], "wrong_answer")
        self.assertEqual(rows[2]["score"], 0.5)
        self.assertEqual(rows[2]["failure_class"], "wrong_answer")
        for row in rows:
            self.assertEqual(set(row), set(ROW_FIELDS))
            self.assertIsInstance(row["checker_exit"], int)
            self.assertIsNone(row["checker_stdout"])
            self.assertIsNone(row["checker_stderr"])

    def test_import_appends_row_fields_and_rejects_duplicate_without_mutation(self):
        fixture = self.fixture()
        output = self.root / "results.jsonl"
        seed = {field: None for field in ROW_FIELDS}
        seed["run_id"] = "seed:task:model:trial1"
        output.write_text(json.dumps(seed) + "\n")

        rows = import_results(fixture.root, output)
        written = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(len(written), 4)
        self.assertEqual(written[1:], rows)
        before = output.read_bytes()
        with self.assertRaisesRegex(HarborResultsError, "already exists"):
            import_results(fixture.root, output)
        self.assertEqual(output.read_bytes(), before)

    def test_corrupt_existing_jsonl_fails_before_append(self):
        fixture = self.fixture()
        output = self.root / "results.jsonl"
        output.write_text('{"run_id":"ok"}\n{broken\n')
        before = output.read_bytes()
        with self.assertRaisesRegex(HarborResultsError, "corrupt JSONL"):
            import_results(fixture.root, output)
        self.assertEqual(output.read_bytes(), before)

    def test_concurrent_imports_cannot_append_duplicate_batches(self):
        fixture = self.fixture()
        output = self.root / "concurrent.jsonl"
        successes = []
        failures = []

        def run_import():
            try:
                successes.append(import_results(fixture.root, output))
            except HarborResultsError as exc:
                failures.append(str(exc))

        threads = [threading.Thread(target=run_import) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("already exists", failures[0])
        self.assertEqual(len(output.read_text().splitlines()), 3)

    def test_append_io_failure_rolls_back_whole_batch(self):
        fixture = self.fixture()
        output = self.root / "rollback.jsonl"
        output.write_text('{"run_id":"seed"}\n')
        before = output.read_bytes()
        with mock.patch("obench.harbor_results.os.fsync", side_effect=[OSError("disk"), None]):
            with self.assertRaisesRegex(HarborResultsError, "rolled back"):
                import_results(fixture.root, output)
        self.assertEqual(output.read_bytes(), before)

    def test_workspace_digest_is_unambiguous_across_file_boundaries(self):
        first = self.root / "tree-one"
        second = self.root / "tree-two"
        first.mkdir()
        second.mkdir()
        (first / "a").write_bytes(b"Xf\0b\0Y")
        (second / "a").write_bytes(b"X")
        (second / "b").write_bytes(b"Y")
        self.assertNotEqual(_tree_digest(first), _tree_digest(second))

    def test_rejects_unresolved_or_wrong_harbor_build(self):
        for field, value in (
            ("version", None),
            ("version", "0.20.1"),
            ("git_commit_hash", None),
            ("git_commit_hash", "0" * 40),
        ):
            with self.subTest(field=field, value=value):
                fixture = GoldenHarborJob(self.root / f"job-{field}-{value}")
                path = fixture.root / "lock.json"
                lock = json.loads(path.read_text())
                lock["harbor"][field] = value
                _write_json(path, lock)
                with self.assertRaises(HarborResultsError):
                    load_rows(fixture.root)

    def test_rejects_missing_evidence_files(self):
        relative_paths = (
            "lock.json",
            "result.json",
            "agent/trajectory.json",
            "verifier/reward.txt",
            "verifier/openbench-verifier-evidence.json",
            "artifacts/manifest.json",
        )
        for index, relative in enumerate(relative_paths):
            with self.subTest(relative=relative):
                fixture = GoldenHarborJob(self.root / f"job-missing-{index}")
                (fixture.trial() / relative).unlink()
                with self.assertRaises(HarborResultsError):
                    load_rows(fixture.root)

    def test_checker_exit_is_authoritative_even_when_partial_score_is_one(self):
        fixture = GoldenHarborJob(
            self.root / "job-nonzero-one",
            specs=[
                {
                    "name": "alpha__one",
                    "task": "alpha",
                    "id": "00000000-0000-0000-0000-000000000001",
                    "score": 1.0,
                    "offset": 0,
                }
            ],
        )
        evidence_path = (
            fixture.trial()
            / "verifier"
            / "openbench-verifier-evidence.json"
        )
        evidence = json.loads(evidence_path.read_text())
        evidence["checker_exit"] = 7
        evidence["parsed_score"] = 1.0
        _write_json(evidence_path, evidence)

        row = load_rows(fixture.root)[0]
        self.assertEqual(row["checker_exit"], 7)
        self.assertEqual(row["score"], 1.0)
        self.assertFalse(row["success"])
        self.assertEqual(row["failure_class"], "wrong_answer")

    def test_rejects_invalid_or_mismatched_verifier_evidence(self):
        for index, mutation in enumerate(
            (
                {"checker_exit": "1"},
                {"reward": 0.25},
                {"checker_exit": 1, "parsed_score": None},
            )
        ):
            with self.subTest(mutation=mutation):
                fixture = GoldenHarborJob(
                    self.root / f"job-evidence-{index}",
                    specs=[
                        {
                            "name": "alpha__one",
                            "task": "alpha",
                            "id": "00000000-0000-0000-0000-000000000001",
                            "score": 1.0,
                            "offset": 0,
                        }
                    ],
                )
                evidence_path = (
                    fixture.trial()
                    / "verifier"
                    / "openbench-verifier-evidence.json"
                )
                evidence = json.loads(evidence_path.read_text())
                evidence.update(mutation)
                _write_json(evidence_path, evidence)
                with self.assertRaises(HarborResultsError):
                    load_rows(fixture.root)

    def test_real_0200_artifact_shape_regression_without_credentials(self):
        fixture = GoldenHarborJob(
            self.root / "vertical-slice-public",
            specs=[
                {
                    "name": "make-it-run__qT2fWKm",
                    "task": "make-it-run",
                    "id": "1c7aae27-b206-449e-bb14-440c861da8f2",
                    "score": 1.0,
                    "offset": 0,
                }
            ],
        )
        lock_path = fixture.trial() / "lock.json"
        trial_lock = json.loads(lock_path.read_text())
        trial_lock["task"]["name"] = "make-it-run"
        trial_lock["agent"]["name"] = "spike_agent:OpenBenchSpikeAgent"
        trial_lock["agent"]["model_name"] = "spike/no-llm"
        _write_json(lock_path, trial_lock)
        job_lock_path = fixture.root / "lock.json"
        job_lock = json.loads(job_lock_path.read_text())
        job_lock["harbor"]["is_editable"] = True
        job_lock["trials"] = [trial_lock]
        _write_json(job_lock_path, job_lock)

        result_path = fixture.trial() / "result.json"
        result = json.loads(result_path.read_text())
        result["source"] = None
        result["task_checksum"] = "c" * 64
        result["config"]["agent"] = {
            "name": "spike_agent:OpenBenchSpikeAgent",
            "model_name": "spike/no-llm",
            "skills": [],
            "resume_trajectory": False,
            "extra_allowed_hosts": [],
            "kwargs": {},
            "mcp_servers": [],
        }
        result["agent_info"] = {
            "name": "openbench-spike",
            "version": "0.1.0",
            "model_info": {"name": "no-llm", "provider": "spike"},
        }
        result["agent_result"] = {
            "n_input_tokens": None,
            "n_cache_tokens": None,
            "n_output_tokens": None,
            "cost_usd": None,
            "rollout_details": None,
            "metadata": {"spike": "custom-base-agent"},
        }
        _write_json(result_path, result)
        _write_json(
            fixture.trial() / "agent" / "trajectory.json",
            {
                "schema_version": "ATIF-v1.7",
                "session_id": result["id"],
                "agent": {
                    "name": "openbench-spike",
                    "version": "0.1.0",
                    "model_name": "spike/no-llm",
                },
                "steps": [
                    {"step_id": 1, "source": "user", "message": "Fix the task."},
                    {
                        "step_id": 2,
                        "source": "agent",
                        "message": "Repaired the task.",
                        "llm_call_count": 0,
                    },
                ],
            },
        )
        job_result_path = fixture.root / "result.json"
        job_result = json.loads(job_result_path.read_text())
        job_result["started_at"] = "2026-07-20T05:59:00"
        job_result["updated_at"] = "2026-07-20T06:04:00"
        job_result["finished_at"] = "2026-07-20T06:05:00"
        job_result["stats"] = {
            "n_completed_trials": 1,
            "n_errored_trials": 0,
            "n_running_trials": 0,
            "n_pending_trials": 0,
            "n_cancelled_trials": 0,
            "n_retries": 0,
            "evals": {
                "openbench-spike__no-llm__adhoc": {
                    "n_trials": 1,
                    "n_errors": 0,
                    "metrics": [{"mean": 1.0}],
                    "pass_at_k": {},
                    "reward_stats": {
                        "reward": {"1.0": ["make-it-run__qT2fWKm"]}
                    },
                    "exception_stats": {},
                }
            },
            "n_input_tokens": None,
            "n_cache_tokens": None,
            "n_output_tokens": None,
            "cost_usd": None,
        }
        _write_json(job_result_path, job_result)

        row = load_rows(fixture.root)[0]
        self.assertEqual(row["harness"], "openbench-spike")
        self.assertEqual(row["model"], "spike/no-llm")
        self.assertEqual(row["token_basis"], "unmetered")
        self.assertEqual(
            row["candidate_provenance"]["harbor_git_commit_hash"],
            "72bc40b1e58b47a9cc6e0f14c29aced3a9e53767",
        )

    def test_rejects_random_incomplete_and_duplicate_trials(self):
        fixture = GoldenHarborJob(self.root / "job-random")
        (fixture.root / "random-directory").mkdir()
        with self.assertRaisesRegex(HarborResultsError, "unexpected directory"):
            load_rows(fixture.root)

        fixture = GoldenHarborJob(self.root / "job-incomplete")
        (fixture.trial() / "result.json").unlink()
        with self.assertRaises(HarborResultsError):
            load_rows(fixture.root)

        fixture = GoldenHarborJob(self.root / "job-duplicate")
        second_path = fixture.trial(1) / "result.json"
        second = json.loads(second_path.read_text())
        second["id"] = fixture.specs[0]["id"]
        _write_json(second_path, second)
        fixture.sync_aggregate()
        with self.assertRaisesRegex(HarborResultsError, "duplicate trial id"):
            load_rows(fixture.root)

    def test_rejects_cross_evidence_tampering(self):
        cases = []

        def trial_lock_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "lock.json"
            lock = json.loads(path.read_text())
            lock["task"]["digest"] = "sha256:" + "f" * 64
            _write_json(path, lock)

        cases.append(("trial lock", trial_lock_tamper))

        def checksum_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "result.json"
            result = json.loads(path.read_text())
            result["task_checksum"] = "f" * 64
            _write_json(path, result)
            fixture.sync_aggregate()

        cases.append(("checksum", checksum_tamper))

        def agent_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "result.json"
            result = json.loads(path.read_text())
            result["agent_info"]["name"] = "other-agent"
            _write_json(path, result)
            fixture.sync_aggregate()

        cases.append(("agent", agent_tamper))

        def environment_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "result.json"
            result = json.loads(path.read_text())
            result["config"]["environment"]["force_build"] = True
            _write_json(path, result)
            fixture.sync_aggregate()

        cases.append(("environment", environment_tamper))

        def reward_tamper(fixture: GoldenHarborJob):
            (fixture.trial() / "verifier" / "reward.txt").write_text("0.25\n")

        cases.append(("reward", reward_tamper))

        def atif_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "agent" / "trajectory.json"
            trajectory = json.loads(path.read_text())
            trajectory["final_metrics"]["total_prompt_tokens"] = 99
            _write_json(path, trajectory)

        cases.append(("ATIF", atif_tamper))

        def manifest_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "artifacts" / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest[1]["status"] = "failed"
            _write_json(path, manifest)

        cases.append(("manifest", manifest_tamper))

        def timing_tamper(fixture: GoldenHarborJob):
            path = fixture.trial() / "result.json"
            result = json.loads(path.read_text())
            result["verifier"]["started_at"] = "2026-07-20T10:00:00+00:00"
            _write_json(path, result)
            fixture.sync_aggregate()

        cases.append(("timing", timing_tamper))

        for index, (name, mutate) in enumerate(cases):
            with self.subTest(name=name):
                fixture = GoldenHarborJob(self.root / f"job-tamper-{index}")
                mutate(fixture)
                with self.assertRaises(HarborResultsError):
                    load_rows(fixture.root)

    def test_rejects_contradictory_job_statistics(self):
        for index, (field, value) in enumerate(
            (
                ("n_errored_trials", 1),
                ("n_cancelled_trials", 1),
                ("n_input_tokens", 299),
                ("n_cache_tokens", 74),
                ("n_output_tokens", 119),
                ("cost_usd", 1.4),
            )
        ):
            with self.subTest(field=field):
                fixture = GoldenHarborJob(self.root / f"job-stats-{index}")
                path = fixture.root / "result.json"
                result = json.loads(path.read_text())
                result["stats"][field] = value
                _write_json(path, result)
                with self.assertRaises(HarborResultsError):
                    load_rows(fixture.root)

    def test_rejects_exception_and_multistep_states(self):
        for field, value in (
            (
                "exception_info",
                {
                    "exception_type": "RuntimeError",
                    "exception_message": "failed",
                    "exception_traceback": "trace",
                    "occurred_at": "2026-07-20T10:02:00+00:00",
                },
            ),
            ("step_results", []),
        ):
            with self.subTest(field=field):
                fixture = GoldenHarborJob(self.root / f"job-{field}")
                path = fixture.trial() / "result.json"
                result = json.loads(path.read_text())
                result[field] = value
                _write_json(path, result)
                fixture.sync_aggregate()
                with self.assertRaises(HarborResultsError):
                    load_rows(fixture.root)

    def test_cli_e2e_imports_and_reports_failures_without_partial_output(self):
        fixture = self.fixture()
        output = self.root / "cli-results.jsonl"
        command = [
            sys.executable,
            "-m",
            "obench.cli",
            "import",
            "harbor-results",
            str(fixture.root),
            "--output",
            str(output),
        ]
        completed = subprocess.run(
            command,
            cwd=self.root,
            env=_SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Imported 3 Harbor trial(s)", completed.stdout)
        self.assertEqual(len(output.read_text().splitlines()), 3)

        bad_fixture = GoldenHarborJob(self.root / "bad-job")
        (bad_fixture.trial() / "agent" / "trajectory.json").unlink()
        bad_output = self.root / "bad-results.jsonl"
        failed = subprocess.run(
            [
                *command[:5],
                str(bad_fixture.root),
                "--output",
                str(bad_output),
            ],
            cwd=self.root,
            env=_SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("ERROR:", failed.stderr)
        self.assertFalse(bad_output.exists())


if __name__ == "__main__":
    unittest.main()
