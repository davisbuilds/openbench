"""End-to-end contract tests for the Harbor-first suite control plane."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from obench import compare, init, stats, suite_run
from obench.harbor_run import HarborBinary
from obench.harbor_results import HARBOR_PROXY_REQUIRED_AGENTS
from obench.suite_run import SuiteRunError


class SuiteRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="obench_suite_run_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _project(self, name: str = "project") -> Path:
        root = self.tmp / name
        root.mkdir()
        init.init_scaffold(root)
        return root

    def _suite_path(self, root: Path) -> Path:
        return root / ".openbench" / "suites" / "default.toml"

    def _suite_text(self, root: Path) -> str:
        return self._suite_path(root).read_text(encoding="utf-8")

    def _write_suite(self, root: Path, text: str) -> None:
        self._suite_path(root).write_text(text, encoding="utf-8")

    def _fake_harbor(self, root: Path) -> HarborBinary:
        launcher = root / "bin" / "harbor"
        launcher.parent.mkdir(exist_ok=True)
        launcher.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        launcher.chmod(0o700)
        return HarborBinary(
            path=launcher,
            version="0.20.0",
            git_commit=init.HARBOR_COMMIT,
            is_editable=False,
        )

    def _custom_probe_result(
        self,
        *,
        version: str = "2.4.1",
        owned: bool = True,
        class_module: str = "acme.harbor",
    ) -> str:
        return json.dumps(
            {
                "class_module": class_module,
                "class_origin_matches_module": True,
                "distribution": "acme-harbor-agent",
                "import_path": "acme.harbor:Agent",
                "owned": owned,
                "version": version,
            }
        )

    def _add_second_local_task_set(
        self,
        root: Path,
        *,
        logical_name: str = "private/second",
    ) -> None:
        task = root / ".openbench" / "second-tasks" / "second"
        task.mkdir(parents=True)
        (task / "instruction.md").write_text("# Second\n", encoding="utf-8")
        (task / "task.toml").write_text(
            'schema_version = "1.4"\n'
            "[task]\n"
            f'name = "{logical_name}"\n'
            'version = "1.0.0"\n',
            encoding="utf-8",
        )
        text = self._suite_text(root).replace(
            "\n[[arms]]\n",
            """\

[[task_sets]]
id = "second"
kind = "local"
path = ".openbench/second-tasks"

[[arms]]
""",
            1,
        )
        self._write_suite(root, text)

    def _add_external_task_set(self, root: Path) -> None:
        text = self._suite_text(root).replace(
            "\n[[arms]]\n",
            """\

[[task_sets]]
id = "registry"
kind = "harbor"
name = "terminal-bench"
ref = "2.1.0"

[[arms]]
""",
            1,
        )
        self._write_suite(root, text)

    def _add_second_stock_arm(self, root: Path) -> None:
        text = self._suite_text(root).replace(
            "\n[run]\n",
            """\

[[arms]]
id = "codex-terra"
harness = "codex"
profile = "local-codex"
model = "gpt-5.6-terra"

[run]
""",
            1,
        )
        self._write_suite(root, text)

    def _add_custom_profile_and_arm(self, root: Path) -> None:
        profile = root / ".openbench" / "profiles" / "acme.toml"
        profile.write_text(
            """\
schema_version = 1
id = "acme"
kind = "custom"
import_path = "acme.harbor:Agent"
distribution = "acme-harbor-agent"
version = "2.4.1"
extra_allowed_hosts = ["api.acme.test"]
concurrency_group = "acme"
concurrency_limit = 1

[models]
"gpt-5.6-terra" = "acme/gpt-5.6-terra-pinned"

[env]
ACME_API_KEY = "${ACME_API_KEY}"

[kwargs]
mode = "strict"
""",
            encoding="utf-8",
        )
        text = self._suite_text(root).replace(
            "\n[run]\n",
            """\

[[arms]]
id = "acme-terra"
harness = "acme"
profile = "acme"
model = "gpt-5.6-terra"

[run]
""",
            1,
        )
        self._write_suite(root, text)

    def _simulated_rows(self, comparison_plan_path: Path):
        plan = json.loads(comparison_plan_path.read_text(encoding="utf-8"))
        plan_sha256 = hashlib.sha256(
            comparison_plan_path.read_bytes()
        ).hexdigest()
        tasks = plan["tasks"] or [comparison_plan_path.stem.split(".", 1)[0]]
        rows = []
        for task in tasks:
            for arm in plan["arms"]:
                for attempt in range(1, plan["attempts"] + 1):
                    proxy_required = (
                        arm["agent_config_name"]
                        in HARBOR_PROXY_REQUIRED_AGENTS
                    )
                    rows.append({
                        "run_id": (
                            f"{plan['job_name']}:{task}:{arm['arm_id']}:{attempt}"
                        ),
                        "ts_iso": "2026-08-04T12:00:00+00:00",
                        "harness": arm["canonical_harness"],
                        "model": arm["canonical_model"],
                        "task": task,
                        "trial": attempt,
                        "success": True,
                        "completed": True,
                        "error": None,
                        "exec_mode": "harbor",
                        "score": 1.0,
                        "token_basis": "harbor_agent_reported",
                        "usage_evidence_grade": (
                            "harbor_reported_proxy_verified"
                            if proxy_required
                            else "harbor_reported"
                        ),
                        "usage_ranking_eligible": True,
                        "candidate_provenance": {
                            "kind": "harbor_job",
                            "comparison_plan_schema_version": plan["schema_version"],
                            "comparison_plan_sha256": plan_sha256,
                            "comparison_plan": plan,
                            "comparison_arm_id": arm["arm_id"],
                            "harbor_agent_config_name": arm[
                                "agent_config_name"
                            ],
                            "agent_config_sha256": arm["agent_config_sha256"],
                            "comparison_resolved_tasks": sorted(tasks),
                            "comparison_block": {
                                "task": task,
                                "index": attempt,
                            },
                            "openbench_verifier_evidence_sha256": "a" * 64,
                            "atif_sha256": "b" * 64,
                            "harbor_metering": (
                                {
                                    "proxy_required": True,
                                    "reconciliation_status": "exact",
                                }
                                if proxy_required
                                else None
                            ),
                            "trial_mapping": "openbench_comparison_plan_v2",
                            "temporal_matched_block_claim": False,
                        },
                    })
        return rows

    def _run_simulated(
        self,
        compiled,
        *,
        returncodes=None,
        importer=None,
    ):
        codes = iter(returncodes or [0] * len(compiled.task_sets))

        def process(argv, **kwargs):
            return SimpleNamespace(returncode=next(codes))

        harbor = HarborBinary(
            path=Path("/fake/harbor"),
            version="0.20.0",
            git_commit=init.HARBOR_COMMIT,
            is_editable=False,
        )
        with mock.patch(
            "obench.suite_run._stage_stock_credentials",
            return_value={},
        ):
            return suite_run.run_suite(
                compiled,
                run_process=process,
                preflight=lambda *args, **kwargs: harbor,
                import_job=importer or (
                    lambda job_path, *, comparison_plan_path: (
                        self._simulated_rows(Path(comparison_plan_path))
                    )
                ),
                finalize=True,
            )

    def test_fresh_init_loads_profile_default_suite_and_discovery(self):
        root = self._project()
        nested = root / "src" / "package"
        nested.mkdir(parents=True)

        compiled = suite_run.compile_suite(start=nested)

        self.assertEqual(compiled.suite.id, "private-default")
        self.assertEqual(compiled.registry.get("local-codex").harness, "codex")
        self.assertEqual(compiled.suite.publication.scope, "local_only")
        self.assertEqual(len(compiled.task_sets), 1)

    def test_explicit_suite_overrides_discovered_default(self):
        root = self._project()
        alternate = root / ".openbench" / "suites" / "alternate.toml"
        alternate.write_text(
            self._suite_text(root).replace(
                'id = "private-default"', 'id = "alternate"', 1
            ),
            encoding="utf-8",
        )

        default = suite_run.compile_suite(start=root)
        explicit = suite_run.compile_suite(alternate, start=root)

        self.assertEqual(default.suite.id, "private-default")
        self.assertEqual(explicit.suite.id, "alternate")

    def test_relocation_preserves_semantic_manifest_digest(self):
        first = self._project("first")
        second = self._project("second")

        left = suite_run.compile_suite(start=first)
        right = suite_run.compile_suite(start=second)

        self.assertEqual(left.manifest, right.manifest)
        self.assertEqual(left.manifest_sha256, right.manifest_sha256)
        rendered = left.manifest_bytes.decode("utf-8")
        self.assertNotIn(str(first), rendered)
        self.assertNotIn(str(second), rendered)

    def test_two_task_sets_and_mixed_profiles_expand_deterministically(self):
        root = self._project()
        self._add_external_task_set(root)
        self._add_custom_profile_and_arm(root)

        compiled = suite_run.compile_suite(start=root)
        jobs = suite_run.plan_jobs(compiled)
        jobs_again = suite_run.plan_jobs(compiled)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            [job.artifact.json_bytes for job in jobs],
            [job.artifact.json_bytes for job in jobs_again],
        )
        for job in jobs:
            agents = job.artifact.as_dict()["agents"]
            self.assertEqual(
                [agent["model_name"] for agent in agents],
                ["gpt-5.6-sol", "acme/gpt-5.6-terra-pinned"],
            )
            self.assertEqual(
                [agent["override_timeout_sec"] for agent in agents],
                [900.0, 900.0],
            )
            plan = job.artifact.comparison_plan.as_dict()
            self.assertEqual(plan["job_config_sha256"], job.artifact.sha256)
            self.assertEqual(
                hashlib.sha256(job.artifact.json_bytes).hexdigest(),
                plan["job_config_sha256"],
            )
            self.assertEqual(
                [arm["arm_id"] for arm in plan["arms"]],
                ["codex-example", "acme-terra"],
            )
        registry_source = jobs[1].artifact.as_dict()["datasets"][0]
        self.assertEqual(
            registry_source,
            {"name": "terminal-bench", "version": "2.1.0"},
        )
        self.assertEqual(
            [arm["id"] for arm in compiled.manifest["arms"]],
            ["codex-example", "acme-terra"],
        )
        self.assertTrue(
            all(arm["agent_config_sha256"] for arm in compiled.manifest["arms"])
        )

    def test_validation_failures_happen_before_staging_or_process(self):
        cases = []

        mismatch = self._project("mismatch")
        self._write_suite(
            mismatch,
            self._suite_text(mismatch).replace(
                'harness = "codex"', 'harness = "pi"', 1
            ),
        )
        cases.append((mismatch, "does not match stock profile"))

        unsupported = self._project("unsupported")
        self._write_suite(
            unsupported,
            self._suite_text(unsupported).replace(
                'model = "gpt-5.6-sol"', 'model = "not-a-model"', 1
            ),
        )
        cases.append((unsupported, "unsupported codex Harbor model"))

        bad_pin = self._project("bad-pin")
        self._write_suite(
            bad_pin,
            self._suite_text(bad_pin).replace(
                'version = "0.20.0"', 'version = "0.21.0"', 1
            ),
        )
        cases.append((bad_pin, "suite Harbor pin"))

        for root, message in cases:
            with self.subTest(case=root.name):
                with mock.patch(
                    "obench.suite_run.HarborOAuthCredential"
                ) as credential, mock.patch(
                    "obench.suite_run.preflight_harbor_binary"
                ) as preflight:
                    with self.assertRaisesRegex(
                        (SuiteRunError, ValueError), message
                    ):
                        suite_run.compile_suite(start=root)
                credential.assert_not_called()
                preflight.assert_not_called()

    def test_missing_custom_env_and_runtime_mismatch_fail_before_auth_or_harbor_run(self):
        root = self._project()
        self._add_custom_profile_and_arm(root)
        compiled = suite_run.compile_suite(start=root)
        preflight = mock.Mock()

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                SuiteRunError, "missing host environment variable ACME_API_KEY"
            ):
                suite_run.run_suite(compiled, preflight=preflight)
        preflight.assert_not_called()

        harbor = self._fake_harbor(root)
        preflight.return_value = harbor
        process = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=self._custom_probe_result(version="2.4.0"),
            )
        )
        with mock.patch.dict(
            os.environ, {"ACME_API_KEY": "not-in-manifest"}, clear=True
        ), mock.patch("obench.suite_run.HarborOAuthCredential") as credential:
            with self.assertRaisesRegex(
                SuiteRunError, "does not match Harbor runtime identity"
            ):
                suite_run.run_suite(
                    compiled,
                    preflight=preflight,
                    run_process=process,
                )
        preflight.assert_called_once()
        credential.assert_not_called()
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[0][:2], [sys.executable, "-c"])
        self.assertNotIn(
            "not-in-manifest",
            json.dumps(process.call_args.kwargs, default=str),
        )
        self.assertNotIn("not-in-manifest", compiled.manifest_bytes.decode())

    def test_custom_profile_rejects_conflicting_harness_labels(self):
        root = self._project()
        self._add_custom_profile_and_arm(root)
        text = self._suite_text(root).replace(
            "\n[run]\n",
            """\

[[arms]]
id = "acme-terra-alias"
harness = "acme-alias"
profile = "acme"
model = "gpt-5.6-terra"

[run]
""",
            1,
        )
        self._write_suite(root, text)

        with self.assertRaisesRegex(
            SuiteRunError, "conflicting harness identities"
        ):
            suite_run.compile_suite(start=root)

    def test_cross_task_set_logical_collision_fails_during_compile(self):
        root = self._project()
        self._add_second_local_task_set(
            root, logical_name="private/example-greeting"
        )

        with self.assertRaisesRegex(SuiteRunError, "duplicated by task sets"):
            suite_run.compile_suite(start=root)

    def test_missing_local_task_name_fails_during_compile(self):
        root = self._project()
        task_toml = (
            root / ".openbench" / "tasks" / "example-greeting" / "task.toml"
        )
        task_toml.write_text(
            task_toml.read_text(encoding="utf-8").replace(
                'name = "private/example-greeting"', 'name = ""'
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SuiteRunError, "task.name"):
            suite_run.compile_suite(start=root)

    def test_mocked_execution_runs_one_harbor_command_per_task_set_and_one_lease(self):
        root = self._project()
        self._add_external_task_set(root)
        self._add_second_stock_arm(root)
        compiled = suite_run.compile_suite(start=root)
        auth_master = root / "auth.json"
        auth_master.write_text('{"tokens":{"access_token":"x"}}\n', encoding="utf-8")
        fake_auth_root = root / "fake-auth"
        process_calls = []
        hook_calls = []

        class FakeCredential:
            entries = 0

            def __init__(self, master):
                self.master = master
                self.input = fake_auth_root / "input.json"
                self.returned = fake_auth_root / "return.json"

            def __enter__(self):
                type(self).entries += 1
                fake_auth_root.mkdir()
                shutil.copyfile(self.master, self.input)
                self.config = SimpleNamespace(
                    auth_json_path=str(self.input),
                    auth_return_path=str(self.returned),
                )
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        def fake_preflight(binary, **kwargs):
            return HarborBinary(
                path=Path("/fake/harbor"),
                version="0.20.0",
                git_commit=init.HARBOR_COMMIT,
                is_editable=False,
            )

        def fake_process(argv, **kwargs):
            self.assertFalse((fake_auth_root / "return.json").exists())
            process_calls.append((argv, kwargs))
            shutil.copyfile(
                fake_auth_root / "input.json",
                fake_auth_root / "return.json",
            )
            return SimpleNamespace(returncode=0)

        with mock.patch(
            "obench.suite_run._resolve_auth_source", return_value=auth_master
        ), mock.patch(
            "obench.suite_run.HarborOAuthCredential", FakeCredential
        ):
            result = suite_run.run_suite(
                compiled,
                run_process=fake_process,
                preflight=fake_preflight,
                post_run_hook=lambda artifacts: hook_calls.append(artifacts),
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(FakeCredential.entries, 1)
        self.assertEqual(len(process_calls), 2)
        self.assertTrue(
            all(call[0][1:3] == ["run", "-c"] for call in process_calls)
        )
        self.assertEqual(len(result.artifacts), 2)
        self.assertEqual(hook_calls, [result.artifacts])
        self.assertEqual(
            result.manifest_path.read_bytes(), compiled.manifest_bytes
        )
        rendered = compiled.manifest_bytes.decode("utf-8")
        self.assertNotIn(str(auth_master), rendered)
        self.assertNotIn(str(root), rendered)
        for artifact in result.artifacts:
            config = json.loads(artifact.config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(config["agents"]), 2)
            self.assertEqual(
                [agent["model_name"] for agent in config["agents"]],
                ["gpt-5.6-sol", "gpt-5.6-terra"],
            )
            self.assertEqual(
                {agent["override_timeout_sec"] for agent in config["agents"]},
                {900.0},
            )
            for manifest_arm, config_agent in zip(
                compiled.manifest["arms"], config["agents"]
            ):
                self.assertEqual(
                    manifest_arm["agent_config_sha256"],
                    suite_run.canonical_agent_config_sha256(config_agent),
                )

    def test_two_task_sets_two_arms_seal_one_results_file_and_compare(self):
        root = self._project()
        self._add_second_local_task_set(root)
        self._add_second_stock_arm(root)
        compiled = suite_run.compile_suite(start=root)

        result = self._run_simulated(compiled)

        self.assertEqual(result.result_count, 4)
        self.assertTrue(result.results_path.is_file())
        self.assertTrue(result.run_manifest_path.is_file())
        self.assertTrue(result.local_record_path.is_file())
        rows = stats.load_rows([result.results_path])
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {
                row["candidate_provenance"]["suite_task_set_id"]
                for row in rows
            },
            {"private", "second"},
        )
        report = compare.build_comparison([str(result.results_path)])
        self.assertEqual(report["matched_n"], 2)
        self.assertEqual(
            report["comparison_identity"], "harbor_suite_manifest"
        )
        sealed = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(sealed["results"]["sha256"], result.results_sha256)
        self.assertEqual(sealed["results"]["count"], 4)
        self.assertEqual(len(sealed["jobs"]), 2)
        self.assertNotIn(str(root), result.run_manifest_path.read_text())
        verified = suite_run.verify_suite_run(result.run_manifest_path)
        self.assertEqual(verified["scope"], "local_only")
        self.assertEqual(verified["result_count"], 4)

    def test_second_job_or_import_failure_leaves_no_results(self):
        for failure in ("job", "import"):
            with self.subTest(failure=failure):
                root = self._project(failure)
                self._add_second_local_task_set(root)
                compiled = suite_run.compile_suite(start=root)
                results_path = (
                    Path(compiled.config.results_dir)
                    / "suite-runs"
                    / f"{compiled.manifest_sha256}.results.jsonl"
                )
                if failure == "job":
                    with self.assertRaisesRegex(
                        SuiteRunError, "not every intended Harbor job"
                    ):
                        self._run_simulated(
                            compiled,
                            returncodes=[0, 9],
                        )
                else:
                    imports = 0

                    def importer(job_path, *, comparison_plan_path):
                        nonlocal imports
                        imports += 1
                        if imports == 2:
                            raise ValueError("simulated import failure")
                        return self._simulated_rows(
                            Path(comparison_plan_path)
                        )

                    with self.assertRaisesRegex(
                        SuiteRunError, "simulated import failure"
                    ):
                        self._run_simulated(compiled, importer=importer)
                self.assertFalse(results_path.exists())

    def test_resume_accepts_exact_outputs_and_rejects_divergence(self):
        root = self._project()
        compiled = suite_run.compile_suite(start=root)
        first = self._run_simulated(compiled)
        second = self._run_simulated(compiled)

        self.assertEqual(first.results_sha256, second.results_sha256)
        self.assertEqual(
            first.run_manifest_path.read_bytes(),
            second.run_manifest_path.read_bytes(),
        )

        first.results_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(SuiteRunError, "divergent existing"):
            self._run_simulated(compiled)

    def test_local_verifier_rejects_config_and_run_manifest_tampering(self):
        root = self._project()
        compiled = suite_run.compile_suite(start=root)
        result = self._run_simulated(compiled)
        config_path = result.artifacts[0].config_path
        original_config = config_path.read_bytes()
        config_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            SuiteRunError, "config or comparison-plan binding"
        ):
            suite_run.verify_suite_run(result.run_manifest_path)
        config_path.write_bytes(original_config)

        manifest = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
        manifest["results"]["count"] += 1
        result.run_manifest_path.write_bytes(
            suite_run._canonical_json(manifest, indent=2)
        )
        with self.assertRaisesRegex(
            SuiteRunError, "results hash or count|results count"
        ):
            suite_run.verify_suite_run(result.run_manifest_path)

    def test_public_smoke_suite_is_rejected(self):
        root = self._project()
        text = self._suite_text(root)
        text = text.replace('id = "private-default"', 'id = "smoke"', 1)
        text = text.replace('scope = "local_only"', 'scope = "public"', 1)
        self._write_suite(root, text)
        with self.assertRaisesRegex(SuiteRunError, "smoke suites"):
            suite_run.compile_suite(start=root)

    def test_suite_manifest_task_set_plan_and_evidence_tampering_fail(self):
        root = self._project()
        compiled = suite_run.compile_suite(start=root)
        result = self._run_simulated(compiled)
        original = stats.load_rows([result.results_path])

        cases = {
            "manifest": lambda row: row["candidate_provenance"][
                "suite_manifest"
            ]["suite"].update({"title": "tampered"}),
            "task_set": lambda row: row["candidate_provenance"].update(
                {"suite_task_set_id": "missing"}
            ),
            "plan": lambda row: row["candidate_provenance"][
                "comparison_plan"
            ].update({"job_name": "tampered"}),
            "trajectory": lambda row: row["candidate_provenance"].update(
                {"atif_sha256": None}
            ),
            "verifier": lambda row: row["candidate_provenance"].update(
                {"openbench_verifier_evidence_sha256": None}
            ),
            "usage": lambda row: row.update(
                {"usage_evidence_grade": "usage_unavailable"}
            ),
            "proxy_usage": lambda row: row["candidate_provenance"].update(
                {"harbor_metering": None}
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                rows = copy.deepcopy(original)
                mutate(rows[0])
                with self.assertRaises(ValueError):
                    stats.validate_suite_rows(rows)

    def test_custom_distribution_executes_after_exact_ownership_verification(self):
        root = self._project()
        self._add_custom_profile_and_arm(root)
        text = self._suite_text(root)
        stock_arm = """\
[[arms]]
id = "codex-example"
harness = "codex"
profile = "local-codex"
model = "gpt-5.6-sol"

"""
        self._write_suite(root, text.replace(stock_arm, "", 1))
        compiled = suite_run.compile_suite(start=root)
        harbor = self._fake_harbor(root)
        harbor_calls = []

        def fake_process(argv, **kwargs):
            if argv[:2] == [sys.executable, "-c"]:
                self.assertEqual(
                    json.loads(kwargs["input"]),
                    {
                        "distribution": "acme-harbor-agent",
                        "import_path": "acme.harbor:Agent",
                    },
                )
                self.assertEqual(
                    kwargs["env"],
                    {
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONNOUSERSITE": "1",
                    },
                )
                return SimpleNamespace(
                    returncode=0,
                    stdout=self._custom_probe_result(),
                )
            harbor_calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(
            os.environ, {"ACME_API_KEY": "runtime-secret"}, clear=True
        ):
            result = suite_run.run_suite(
                compiled,
                run_process=fake_process,
                preflight=lambda *args, **kwargs: harbor,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(harbor_calls), 1)
        config = json.loads(
            result.artifacts[0].config_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            config["agents"][0]["env"],
            {"ACME_API_KEY": "${ACME_API_KEY}"},
        )
        self.assertNotIn("runtime-secret", compiled.manifest_bytes.decode())

    def test_custom_runtime_rejects_shadowed_module_and_wrong_interpreter(self):
        root = self._project()
        self._add_custom_profile_and_arm(root)
        text = self._suite_text(root)
        stock_arm = """\
[[arms]]
id = "codex-example"
harness = "codex"
profile = "local-codex"
model = "gpt-5.6-sol"

"""
        self._write_suite(root, text.replace(stock_arm, "", 1))
        compiled = suite_run.compile_suite(start=root)
        harbor = self._fake_harbor(root)

        with mock.patch.dict(
            os.environ, {"ACME_API_KEY": "runtime-secret"}, clear=True
        ):
            with self.assertRaisesRegex(
                SuiteRunError, "does not match Harbor runtime identity"
            ):
                suite_run.run_suite(
                    compiled,
                    run_process=lambda *args, **kwargs: SimpleNamespace(
                        returncode=0,
                        stdout=self._custom_probe_result(owned=False),
                    ),
                    preflight=lambda *args, **kwargs: harbor,
                )

            harbor.path.write_text("#!/bin/sh\n", encoding="utf-8")
            process = mock.Mock(
                return_value=SimpleNamespace(returncode=2, stdout="")
            )
            with self.assertRaisesRegex(
                SuiteRunError, "cannot be imported by Harbor's Python interpreter"
            ):
                suite_run.run_suite(
                    compiled,
                    run_process=process,
                    preflight=lambda *args, **kwargs: harbor,
                )
            self.assertEqual(process.call_args.args[0][:2], ["/bin/sh", "-c"])

            harbor.path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with self.assertRaisesRegex(
                SuiteRunError, "cannot prove.*interpreter"
            ):
                suite_run.run_suite(
                    compiled,
                    run_process=mock.Mock(),
                    preflight=lambda *args, **kwargs: harbor,
                )

    def test_zero_exit_resume_seeds_return_only_after_harbor(self):
        root = self._project()
        compiled = suite_run.compile_suite(start=root)
        planned = suite_run.plan_jobs(compiled)[0].artifact
        job_path = planned.jobs_dir / planned.job_name
        job_path.mkdir(parents=True)
        (job_path / "config.json").write_text("{}\n", encoding="utf-8")
        (job_path / "result.json").write_text(
            json.dumps(
                {
                    "finished_at": "2026-08-04T12:00:00+00:00",
                    "n_total_trials": 1,
                    "stats": {
                        "n_completed_trials": 1,
                        "n_running_trials": 0,
                        "n_pending_trials": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        auth_master = root / "auth.json"
        auth_master.write_text('{"tokens":{"access_token":"x"}}\n', encoding="utf-8")
        fake_auth_root = root / "fake-auth"
        observed_return_before_harbor = []

        class FakeCredential:
            def __init__(self, master):
                self.master = master

            def __enter__(self):
                fake_auth_root.mkdir()
                input_path = fake_auth_root / "input.json"
                shutil.copyfile(self.master, input_path)
                self.config = SimpleNamespace(
                    auth_json_path=str(input_path),
                    auth_return_path=str(fake_auth_root / "return.json"),
                )
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        def fake_process(argv, **kwargs):
            observed_return_before_harbor.append(
                (fake_auth_root / "return.json").exists()
            )
            return SimpleNamespace(returncode=0)

        with mock.patch(
            "obench.suite_run._resolve_auth_source", return_value=auth_master
        ), mock.patch(
            "obench.suite_run.HarborOAuthCredential", FakeCredential
        ):
            result = suite_run.run_suite(
                compiled,
                run_process=fake_process,
                preflight=lambda *args, **kwargs: HarborBinary(
                    path=Path("/fake/harbor"),
                    version="0.20.0",
                    git_commit=init.HARBOR_COMMIT,
                    is_editable=False,
                ),
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(observed_return_before_harbor, [False])
        self.assertTrue((fake_auth_root / "return.json").is_file())

    def test_generated_artifact_directories_reject_symlink_escape(self):
        root = self._project()
        compiled = suite_run.compile_suite(start=root)
        outside = self.tmp / "outside"
        outside.mkdir()
        suite_runs = root / ".openbench" / "results" / "suite-runs"
        suite_runs.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(SuiteRunError, "unsafe component"):
            suite_run._write_manifest(compiled)
        self.assertEqual(list(outside.iterdir()), [])

        suite_runs.unlink()
        suite_configs = root / ".openbench" / "jobs" / "suite-configs"
        suite_configs.symlink_to(outside, target_is_directory=True)
        credential = mock.Mock()
        with mock.patch(
            "obench.suite_run.HarborOAuthCredential", credential
        ):
            with self.assertRaisesRegex(SuiteRunError, "unsafe component"):
                suite_run.run_suite(
                    compiled,
                    preflight=lambda *args, **kwargs: HarborBinary(
                        path=Path("/fake/harbor"),
                        version="0.20.0",
                        git_commit=init.HARBOR_COMMIT,
                        is_editable=False,
                    ),
                )
        credential.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_nonzero_harbor_exit_is_returned_and_stops_later_jobs(self):
        root = self._project()
        self._add_external_task_set(root)
        compiled = suite_run.compile_suite(start=root)
        auth_master = root / "auth.json"
        auth_master.write_text('{"tokens":{"access_token":"x"}}\n', encoding="utf-8")
        fake_auth_root = root / "fake-auth"
        calls = []

        class FakeCredential:
            def __init__(self, master):
                self.master = master

            def __enter__(self):
                fake_auth_root.mkdir()
                input_path = fake_auth_root / "input.json"
                shutil.copyfile(self.master, input_path)
                self.config = SimpleNamespace(
                    auth_json_path=str(input_path),
                    auth_return_path=str(fake_auth_root / "return.json"),
                )
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        def fake_process(argv, **kwargs):
            calls.append(argv)
            shutil.copyfile(
                fake_auth_root / "input.json",
                fake_auth_root / "return.json",
            )
            return SimpleNamespace(returncode=23)

        with mock.patch(
            "obench.suite_run._resolve_auth_source", return_value=auth_master
        ), mock.patch(
            "obench.suite_run.HarborOAuthCredential", FakeCredential
        ):
            result = suite_run.run_suite(
                compiled,
                run_process=fake_process,
                preflight=lambda *args, **kwargs: HarborBinary(
                    path=Path("/fake/harbor"),
                    version="0.20.0",
                    git_commit=init.HARBOR_COMMIT,
                    is_editable=False,
                ),
            )

        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(len(calls), 1)

    def test_second_job_cannot_reuse_first_jobs_oauth_return(self):
        root = self._project()
        self._add_external_task_set(root)
        compiled = suite_run.compile_suite(start=root)
        auth_master = root / "auth.json"
        auth_master.write_text('{"generation":0}\n', encoding="utf-8")
        fake_auth_root = root / "fake-auth"
        calls = []

        class FakeCredential:
            def __init__(self, master):
                self.master = master

            def __enter__(self):
                fake_auth_root.mkdir()
                shutil.copyfile(self.master, fake_auth_root / "input.json")
                self.config = SimpleNamespace(
                    auth_json_path=str(fake_auth_root / "input.json"),
                    auth_return_path=str(fake_auth_root / "return.json"),
                )
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        def fake_process(argv, **kwargs):
            calls.append(argv)
            self.assertFalse((fake_auth_root / "return.json").exists())
            if len(calls) == 1:
                (fake_auth_root / "return.json").write_text(
                    '{"generation":1}\n', encoding="utf-8"
                )
            else:
                self.assertEqual(
                    (fake_auth_root / "input.json").read_text(encoding="utf-8"),
                    '{"generation":1}\n',
                )
            return SimpleNamespace(returncode=0)

        with mock.patch(
            "obench.suite_run._resolve_auth_source", return_value=auth_master
        ), mock.patch(
            "obench.suite_run.HarborOAuthCredential", FakeCredential
        ):
            with self.assertRaisesRegex(
                SuiteRunError, "without producing fresh staged OAuth"
            ):
                suite_run.run_suite(
                    compiled,
                    run_process=fake_process,
                    preflight=lambda *args, **kwargs: HarborBinary(
                        path=Path("/fake/harbor"),
                        version="0.20.0",
                        git_commit=init.HARBOR_COMMIT,
                        is_editable=False,
                    ),
                )

        self.assertEqual(len(calls), 2)
        self.assertFalse((fake_auth_root / "return.json").exists())

    def test_bare_registry_name_rejects_non_version_immutable_ref(self):
        root = self._project()
        self._add_external_task_set(root)
        self._write_suite(
            root,
            self._suite_text(root).replace(
                'ref = "2.1.0"',
                f'ref = "{"a" * 40}"',
                1,
            ),
        )
        compiled = suite_run.compile_suite(start=root)

        with self.assertRaisesRegex(
            SuiteRunError, "requires an exact semantic version"
        ):
            suite_run.plan_jobs(compiled)

    def test_cli_plan_from_nested_directory_emits_no_artifacts(self):
        root = self._project()
        nested = root / "src"
        nested.mkdir()
        prior = Path.cwd()
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            os.chdir(nested)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                returncode = suite_run.main(["--plan"])
        finally:
            os.chdir(prior)

        self.assertEqual(returncode, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["manifest_sha256"],
            suite_run.compile_suite(start=root).manifest_sha256,
        )
        self.assertFalse(
            (root / ".openbench" / "results" / "suite-runs").exists()
        )
        self.assertFalse(
            (root / ".openbench" / "jobs" / "suite-configs").exists()
        )


if __name__ == "__main__":
    unittest.main()
