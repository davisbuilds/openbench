import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from obench import harbor_job as hj


class HarborJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="obench-harbor-job-")
        self.root = Path(self.tmp.name)
        self.task_set = self.root / "exported"
        self.task_set.mkdir()
        self._make_task("zeta")
        self._make_task("alpha")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_task(self, name, *, config=True, instruction=True):
        task = self.task_set / name
        task.mkdir()
        if config:
            (task / "task.toml").write_text('schema_version = "1.4"\n')
        if instruction:
            (task / "instruction.md").write_text(f"# {name}\n")
        return task

    def _local_spec(self, **changes):
        spec = hj.HarborJobSpec(
            job_name="openbench-smoke",
            jobs_dir=self.root / "jobs",
            source=hj.LocalTaskSet(self.task_set),
            agent_profiles=(hj.AgentProfile(profile_id="codex", name="codex"),),
            models=("openai/gpt-5",),
            attempts=2,
            concurrency=hj.ConcurrencyPolicy(n_concurrent_trials=3),
            retry=hj.RetryPolicy(max_retries=1),
        )
        return replace(spec, **changes)

    def test_deterministic_golden_native_job_config(self):
        self.assertEqual(hj.HARBOR_VERSION, "0.20.0")
        self.assertEqual(
            hj.HARBOR_GIT_COMMIT,
            "72bc40b1e58b47a9cc6e0f14c29aced3a9e53767",
        )
        self.assertIn(hj.HARBOR_GIT_COMMIT, hj.HARBOR_JOB_CONFIG_SOURCE)
        spec = hj.HarborJobSpec(
            job_name="community-proof",
            jobs_dir="/tmp/openbench-harbor-jobs",
            source=hj.Dataset(
                name="openbench/core-smoke",
                ref="sha256:" + "0" * 64,
                task_names=("make-it-run", "fix-tests"),
            ),
            agent_profiles=(
                hj.AgentProfile(
                    profile_id="candidate",
                    import_path="company.harbor:CandidateAgent",
                    n_concurrent=2,
                    concurrency_group="company-api",
                    kwargs={"mode": "strict", "temperature": 0},
                    env={"OPENAI_BASE_URL": "${OPENBENCH_PROXY_URL}"},
                    extra_allowed_hosts=("proxy.internal",),
                ),
            ),
            models=("openai/gpt-5",),
            attempts=3,
            concurrency=hj.ConcurrencyPolicy(n_concurrent_trials=4),
            retry=hj.RetryPolicy(
                max_retries=2,
                include_exceptions=("EnvironmentStartError",),
                exclude_exceptions=("AgentAuthenticationError", "ModelNotFoundError"),
                wait_multiplier=2,
                min_wait_sec=3,
                max_wait_sec=30,
            ),
        )

        artifact = hj.build_job_config(spec)

        expected = """{
  "agents": [
    {
      "concurrency_group": "company-api",
      "env": {
        "OPENAI_BASE_URL": "${OPENBENCH_PROXY_URL}"
      },
      "extra_allowed_hosts": [
        "proxy.internal"
      ],
      "import_path": "company.harbor:CandidateAgent",
      "kwargs": {
        "mode": "strict",
        "temperature": 0
      },
      "model_name": "openai/gpt-5",
      "n_concurrent": 2
    }
  ],
  "datasets": [
    {
      "name": "openbench/core-smoke",
      "ref": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "task_names": [
        "make-it-run",
        "fix-tests"
      ]
    }
  ],
  "job_name": "community-proof",
  "jobs_dir": "/tmp/openbench-harbor-jobs",
  "n_attempts": 3,
  "n_concurrent_trials": 4,
  "retry": {
    "exclude_exceptions": [
      "AgentAuthenticationError",
      "ModelNotFoundError"
    ],
    "include_exceptions": [
      "EnvironmentStartError"
    ],
    "max_retries": 2,
    "max_wait_sec": 30.0,
    "min_wait_sec": 3.0,
    "wait_multiplier": 2.0
  },
  "tasks": []
}
"""
        self.assertEqual(artifact.json_bytes.decode(), expected)
        self.assertEqual(
            artifact.sha256,
            hashlib.sha256(expected.encode()).hexdigest(),
        )
        self.assertEqual(artifact.as_dict(), json.loads(expected))
        self.assertIsNone(artifact.trial_count)
        self.assertEqual(
            artifact.comparison_plan.as_dict()["dataset"],
            {
                "name": "openbench/core-smoke",
                "ref": "sha256:" + "0" * 64,
                "task_names": ["make-it-run", "fix-tests"],
            },
        )
        self.assertIsNone(artifact.comparison_plan.as_dict()["tasks"])

    def test_local_task_set_expands_profiles_models_and_attempts(self):
        profiles = (
            hj.AgentProfile(profile_id="codex", name="codex"),
            hj.AgentProfile(
                profile_id="custom",
                import_path="acme.agent:Agent",
                n_concurrent=2,
                env={"ACME_CONFIG": "${ACME_CONFIG_PATH}"},
            ),
        )
        artifact = hj.build_job_config(
            self._local_spec(
                agent_profiles=profiles,
                models=("provider/model-b", "provider/model-a"),
                attempts=3,
            )
        )
        config = artifact.as_dict()

        self.assertEqual(
            config["datasets"],
            [
                {
                    "path": str(self.task_set.resolve()),
                    "task_names": ["alpha", "zeta"],
                }
            ],
        )
        self.assertEqual(config["tasks"], [])
        self.assertEqual(
            [(agent.get("name", agent.get("import_path")), agent["model_name"])
             for agent in config["agents"]],
            [
                ("codex", "provider/model-b"),
                ("codex", "provider/model-a"),
                ("acme.agent:Agent", "provider/model-b"),
                ("acme.agent:Agent", "provider/model-a"),
            ],
        )
        self.assertEqual(artifact.source_task_count, 2)
        self.assertEqual(artifact.trial_count, 24)
        self.assertIsNotNone(artifact.comparison_plan)
        comparison_plan = artifact.comparison_plan.as_dict()
        self.assertEqual(
            comparison_plan,
            {
                "schema_version": hj.COMPARISON_PLAN_SCHEMA_VERSION,
                "harbor_version": hj.HARBOR_VERSION,
                "harbor_git_commit_hash": hj.HARBOR_GIT_COMMIT,
                "job_name": "openbench-smoke",
                "job_config_sha256": artifact.sha256,
                "attempts": 3,
                "dataset": None,
                "tasks": ["alpha", "zeta"],
                "arms": [
                    {
                        "arm_id": "codex@provider/model-b",
                        "agent_config_name": "codex",
                        "harbor_model_name": "provider/model-b",
                        "agent_config_sha256": (
                            hj.canonical_agent_config_sha256(config["agents"][0])
                        ),
                        "canonical_harness": "codex",
                        "canonical_model": "provider/model-b",
                    },
                    {
                        "arm_id": "codex@provider/model-a",
                        "agent_config_name": "codex",
                        "harbor_model_name": "provider/model-a",
                        "agent_config_sha256": (
                            hj.canonical_agent_config_sha256(config["agents"][1])
                        ),
                        "canonical_harness": "codex",
                        "canonical_model": "provider/model-a",
                    },
                    {
                        "arm_id": "custom@provider/model-b",
                        "agent_config_name": "acme.agent:Agent",
                        "harbor_model_name": "provider/model-b",
                        "agent_config_sha256": (
                            hj.canonical_agent_config_sha256(config["agents"][2])
                        ),
                        "canonical_harness": "custom",
                        "canonical_model": "provider/model-b",
                    },
                    {
                        "arm_id": "custom@provider/model-a",
                        "agent_config_name": "acme.agent:Agent",
                        "harbor_model_name": "provider/model-a",
                        "agent_config_sha256": (
                            hj.canonical_agent_config_sha256(config["agents"][3])
                        ),
                        "canonical_harness": "custom",
                        "canonical_model": "provider/model-a",
                    },
                ],
            },
        )
        self.assertEqual(
            artifact.comparison_plan.sha256,
            hashlib.sha256(artifact.comparison_plan.json_bytes).hexdigest(),
        )
        self.assertEqual(artifact.json_bytes, hj.build_job_config(
            self._local_spec(
                agent_profiles=profiles,
                models=("provider/model-b", "provider/model-a"),
                attempts=3,
            )
        ).json_bytes)

    def test_profiles_can_bind_distinct_exact_model_names(self):
        profiles = (
            hj.AgentProfile(
                profile_id="codex",
                import_path="obench.harbor_agents.codex:OpenBenchCodexOAuth",
                model_name="gpt-5.6-sol",
            ),
            hj.AgentProfile(
                profile_id="pi",
                import_path="obench.harbor_agents.pi:OpenBenchPiOAuth",
                model_name="openai-codex/gpt-5.6-sol",
            ),
        )
        artifact = hj.build_job_config(
            self._local_spec(
                agent_profiles=profiles,
                models=(),
                attempts=2,
            )
        )
        self.assertEqual(
            [
                (agent["import_path"], agent["model_name"])
                for agent in artifact.as_dict()["agents"]
            ],
            [
                (
                    "obench.harbor_agents.codex:OpenBenchCodexOAuth",
                    "gpt-5.6-sol",
                ),
                (
                    "obench.harbor_agents.pi:OpenBenchPiOAuth",
                    "openai-codex/gpt-5.6-sol",
                ),
            ],
        )
        self.assertEqual(artifact.trial_count, 8)

    def test_same_agent_and_model_variants_bind_full_config_and_labels(self):
        profiles = (
            hj.AgentProfile(
                profile_id="strict",
                arm_id="strict-arm",
                canonical_harness="acme-strict",
                canonical_model="model-x",
                import_path="acme.agent:Agent",
                model_name="provider/model-x",
                kwargs={"mode": "strict"},
            ),
            hj.AgentProfile(
                profile_id="fast",
                arm_id="fast-arm",
                canonical_harness="acme-fast",
                canonical_model="model-x",
                import_path="acme.agent:Agent",
                model_name="provider/model-x",
                kwargs={"mode": "fast"},
            ),
        )

        artifact = hj.build_job_config(
            self._local_spec(agent_profiles=profiles, models=(), attempts=1)
        )
        arms = artifact.comparison_plan.as_dict()["arms"]

        self.assertEqual([arm["arm_id"] for arm in arms], ["strict-arm", "fast-arm"])
        self.assertEqual(
            [arm["canonical_harness"] for arm in arms],
            ["acme-strict", "acme-fast"],
        )
        self.assertEqual(
            len({arm["agent_config_sha256"] for arm in arms}),
            2,
        )
        self.assertEqual(
            [
                arm["agent_config_sha256"]
                for arm in arms
            ],
            [
                hj.canonical_agent_config_sha256(agent)
                for agent in artifact.as_dict()["agents"]
            ],
        )

    def test_registry_and_package_dataset_plans_bind_exact_descriptors(self):
        cases = (
            (
                hj.Dataset(
                    name="terminal-bench",
                    version="2.0",
                    task_names=("task-b", "task-a"),
                ),
                {
                    "name": "terminal-bench",
                    "version": "2.0",
                    "task_names": ["task-b", "task-a"],
                },
            ),
            (
                hj.Dataset(
                    name="openbench/core",
                    ref="sha256:" + "1" * 64,
                    task_names=("task-a",),
                ),
                {
                    "name": "openbench/core",
                    "ref": "sha256:" + "1" * 64,
                    "task_names": ["task-a"],
                },
            ),
        )
        for source, descriptor in cases:
            with self.subTest(source=source):
                artifact = hj.build_job_config(self._local_spec(source=source))
                plan = artifact.comparison_plan.as_dict()
                self.assertEqual(plan["dataset"], descriptor)
                self.assertIsNone(plan["tasks"])
                self.assertEqual(artifact.as_dict()["datasets"], [descriptor])

    def test_profile_without_model_and_no_shared_models_is_rejected(self):
        with self.assertRaisesRegex(hj.HarborJobError, "requires model_name"):
            hj.build_job_config(self._local_spec(models=()))

    def test_local_task_selection_is_sorted_and_exact(self):
        artifact = hj.build_job_config(
            self._local_spec(
                source=hj.LocalTaskSet(self.task_set, task_names=("zeta", "alpha"))
            )
        )
        self.assertEqual(
            artifact.as_dict()["datasets"][0]["task_names"], ["alpha", "zeta"]
        )

    def test_local_source_rejects_one_task_partial_and_unknown_selection(self):
        with self.assertRaisesRegex(hj.HarborJobError, "points to one task"):
            hj.build_job_config(self._local_spec(source=hj.LocalTaskSet(
                self.task_set / "alpha"
            )))

        self._make_task("partial", instruction=False)
        with self.assertRaisesRegex(hj.HarborJobError, "partial Harbor tasks"):
            hj.build_job_config(self._local_spec())

        (self.task_set / "partial").rename(self.root / "partial-away")
        with self.assertRaisesRegex(hj.HarborJobError, "selected tasks"):
            hj.build_job_config(self._local_spec(source=hj.LocalTaskSet(
                self.task_set, task_names=("missing",)
            )))

    def test_dataset_requires_immutable_unambiguous_reference(self):
        cases = (
            hj.Dataset(name="terminal-bench"),
            hj.Dataset(name="terminal-bench", ref="main"),
            hj.Dataset(name="org/tasks"),
            hj.Dataset(name="org/tasks", ref="latest"),
            hj.Dataset(name="org/tasks", version="2", ref="sha256:abc"),
        )
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(hj.HarborJobError):
                    hj.build_job_config(self._local_spec(source=source))

    def test_agent_profile_requires_one_identity_and_safe_runtime_hooks(self):
        cases = (
            hj.AgentProfile(profile_id="none"),
            hj.AgentProfile(
                profile_id="both", name="codex", import_path="acme.agent:Agent"
            ),
            hj.AgentProfile(profile_id="bad-import", import_path="acme.agent"),
            hj.AgentProfile(
                profile_id="literal", name="codex", env={"TOKEN": "secret"}
            ),
            hj.AgentProfile(
                profile_id="secret-kwarg", name="codex", kwargs={"api_token": "x"}
            ),
        )
        for profile in cases:
            with self.subTest(profile=profile.profile_id):
                with self.assertRaises(hj.HarborJobError):
                    hj.build_job_config(self._local_spec(agent_profiles=(profile,)))

    def test_rejects_duplicate_or_partial_matrix_dimensions(self):
        with self.assertRaisesRegex(hj.HarborJobError, "models must not contain"):
            hj.build_job_config(self._local_spec(models=("model", "model")))
        with self.assertRaisesRegex(hj.HarborJobError, "attempts"):
            hj.build_job_config(self._local_spec(attempts=0))
        duplicate_profiles = (
            hj.AgentProfile(profile_id="same", name="codex"),
            hj.AgentProfile(profile_id="same", name="pi"),
        )
        with self.assertRaisesRegex(hj.HarborJobError, "duplicate profile_id"):
            hj.build_job_config(
                self._local_spec(agent_profiles=duplicate_profiles)
            )

    def test_rejects_concurrency_conflicts(self):
        too_large = hj.AgentProfile(
            profile_id="large", name="codex", n_concurrent=4
        )
        with self.assertRaisesRegex(hj.HarborJobError, "between 1 and 3"):
            hj.build_job_config(self._local_spec(agent_profiles=(too_large,)))

        profiles = (
            hj.AgentProfile(
                profile_id="one", name="codex", n_concurrent=1,
                concurrency_group="shared"
            ),
            hj.AgentProfile(
                profile_id="two", name="pi", n_concurrent=2,
                concurrency_group="shared"
            ),
        )
        with self.assertRaisesRegex(hj.HarborJobError, "conflicting limits"):
            hj.build_job_config(self._local_spec(agent_profiles=profiles))

    def test_rejects_ambiguous_or_invalid_retry_policy(self):
        overlap = hj.RetryPolicy(
            max_retries=1,
            include_exceptions=("AgentTimeoutError",),
            exclude_exceptions=("AgentTimeoutError",),
        )
        with self.assertRaisesRegex(hj.HarborJobError, "must not overlap"):
            hj.build_job_config(self._local_spec(retry=overlap))

        backwards = hj.RetryPolicy(
            max_retries=1, min_wait_sec=10, max_wait_sec=2
        )
        with self.assertRaisesRegex(hj.HarborJobError, "must not exceed"):
            hj.build_job_config(self._local_spec(retry=backwards))

    def test_write_is_atomic_idempotent_and_refuses_replacement(self):
        artifact = hj.build_job_config(self._local_spec())
        path = self.root / "configs" / "job.json"
        comparison_path = hj.comparison_plan_path_for_config(path)

        self.assertEqual(hj.write_job_config(artifact, path), path.resolve())
        self.assertEqual(hj.write_job_config(artifact, path), path.resolve())
        self.assertEqual(path.read_bytes(), artifact.json_bytes)
        self.assertEqual(
            comparison_path,
            self.root / "configs" / "job.openbench-comparison-plan.json",
        )
        self.assertEqual(
            hj.write_comparison_plan(artifact.comparison_plan, comparison_path),
            comparison_path.resolve(),
        )
        self.assertEqual(
            hj.write_comparison_plan(artifact.comparison_plan, comparison_path),
            comparison_path.resolve(),
        )

        different = hj.build_job_config(self._local_spec(attempts=3))
        with self.assertRaisesRegex(hj.HarborJobError, "refusing to overwrite"):
            hj.write_job_config(different, path)
        with self.assertRaisesRegex(hj.HarborJobError, "refusing to overwrite"):
            hj.write_comparison_plan(
                different.comparison_plan,
                comparison_path,
            )

    def test_rejects_comparison_arms_with_ambiguous_lock_identity(self):
        profiles = (
            hj.AgentProfile(profile_id="first", name="codex"),
            hj.AgentProfile(profile_id="second", name="codex"),
        )
        with self.assertRaisesRegex(
            hj.HarborJobError,
            "distinct rendered agent configs",
        ):
            hj.build_job_config(
                self._local_spec(agent_profiles=profiles)
            )

    def test_command_plan_binds_digest_and_uses_native_config_command(self):
        artifact = hj.build_job_config(self._local_spec())
        config_path = hj.write_job_config(artifact, self.root / "job.json")

        plan = hj.build_command_plan(
            artifact, config_path, harbor_binary="/opt/harbor-0.20.0/bin/harbor"
        )

        self.assertEqual(
            plan.argv,
            ("/opt/harbor-0.20.0/bin/harbor", "run", "-c", str(config_path)),
        )
        self.assertEqual(plan.config_sha256, artifact.sha256)
        self.assertEqual(plan.expected_job_path, self.root / "jobs" / "openbench-smoke")
        self.assertFalse(plan.resumes_existing_job)
        self.assertEqual(plan.harbor_version, "0.20.0")

    def test_command_plan_delegates_partial_and_complete_resume_state_to_harbor(self):
        artifact = hj.build_job_config(self._local_spec())
        config_path = hj.write_job_config(artifact, self.root / "job.json")
        job_path = self.root / "jobs" / "openbench-smoke"
        job_path.mkdir(parents=True)

        plan = hj.build_command_plan(artifact, config_path)
        self.assertFalse(plan.resumes_existing_job)

        (job_path / "config.json").write_text("{}\n")
        plan = hj.build_command_plan(artifact, config_path)
        self.assertTrue(plan.resumes_existing_job)
        (job_path / "lock.json").write_text("{}\n")
        plan = hj.build_command_plan(artifact, config_path)
        self.assertTrue(plan.resumes_existing_job)
        self.assertEqual(plan.argv[:3], ("harbor", "run", "-c"))

    def test_command_plan_rejects_modified_or_symlinked_config(self):
        artifact = hj.build_job_config(self._local_spec())
        config_path = hj.write_job_config(artifact, self.root / "job.json")
        config_path.write_text("{}\n")
        with self.assertRaisesRegex(hj.HarborJobError, "does not match"):
            hj.build_command_plan(artifact, config_path)

        link = self.root / "linked.json"
        link.symlink_to(config_path)
        with self.assertRaisesRegex(hj.HarborJobError, "symlink"):
            hj.build_command_plan(artifact, link)

    def test_write_rejects_symlinked_config(self):
        artifact = hj.build_job_config(self._local_spec())
        target = self.root / "target.json"
        target.write_text("{}\n")
        link = self.root / "linked-write.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(hj.HarborJobError, "symlink"):
            hj.write_job_config(artifact, link)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_local_task_set_rejects_symlink_root(self):
        link = self.root / "export-link"
        link.symlink_to(self.task_set, target_is_directory=True)
        with self.assertRaisesRegex(hj.HarborJobError, "must not be a symlink"):
            hj.build_job_config(self._local_spec(source=hj.LocalTaskSet(link)))


if __name__ == "__main__":
    unittest.main()
