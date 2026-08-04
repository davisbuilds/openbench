"""Contract tests for strict Harbor profile specs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from obench import harbor_job as hj
from obench.profile_spec import (
    CustomProfileSpec,
    ProfileSpecError,
    StockProfileSpec,
    compile_profile,
    load_profile,
    load_profile_registry,
)


class ProfileSpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="obench_profile_spec_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profiles = self.tmp / ".openbench" / "profiles"
        self.profiles.mkdir(parents=True)

    def _write(self, profile_id: str, contents: str) -> Path:
        path = self.profiles / f"{profile_id}.toml"
        path.write_text(contents, encoding="utf-8")
        return path

    def _stock(self, profile_id: str = "local-codex", harness: str = "codex") -> str:
        return f"""\
schema_version = 1
id = "{profile_id}"
kind = "stock"
harness = "{harness}"
"""

    def _custom(self, profile_id: str = "acme-agent") -> str:
        return f"""\
schema_version = 1
id = "{profile_id}"
kind = "custom"
import_path = "acme.harbor.agent:AcmeAgent"
distribution = "acme-harbor-agent"
version = "2.4.1"
extra_allowed_hosts = ["api.acme.test", "10.0.0.8"]
concurrency_group = "acme-api"
concurrency_limit = 2

[models]
"gpt-5.6-sol" = "openai/gpt-5.6-sol-2026-08-01"
"gpt-5.6-terra" = "openai/gpt-5.6-terra-2026-08-01"

[env]
ACME_API_KEY = "${{ACME_API_KEY}}"
ACME_BASE_URL = "${{ACME_BASE_URL}}"

[kwargs]
mode = "strict"
temperature = 0
features = ["tools", "atif"]
"""

    def test_loads_and_compiles_stock_profile_through_canonical_resolver(self):
        spec = load_profile(self._write("local-codex", self._stock()))

        self.assertIsInstance(spec, StockProfileSpec)
        compiled = compile_profile(spec, "gpt-5.6-sol")
        self.assertEqual(compiled.profile_id, "local-codex")
        self.assertEqual(
            compiled.import_path,
            "obench.harbor_agents.codex_profile:OpenBenchCodexOAuthProfile",
        )
        self.assertEqual(compiled.model_name, "gpt-5.6-sol")
        self.assertEqual(compiled.kwargs["version"], "0.144.5")
        self.assertEqual(compiled.kwargs["reasoning_effort"], "medium")
        self.assertEqual(compiled.n_concurrent, 1)
        self.assertEqual(compiled.concurrency_group, "openbench-oauth-codex")
        self.assertEqual(
            compiled.env,
            {
                "CODEX_AUTH_JSON_PATH": "${CODEX_AUTH_JSON_PATH}",
                "OPENBENCH_CODEX_AUTH_RETURN_PATH": (
                    "${OPENBENCH_CODEX_AUTH_RETURN_PATH}"
                ),
            },
        )

    def test_loads_and_compiles_custom_profile_with_exact_model_mapping(self):
        spec = load_profile(self._write("acme-agent", self._custom()))

        self.assertIsInstance(spec, CustomProfileSpec)
        self.assertEqual(spec.distribution, "acme-harbor-agent")
        compiled = compile_profile(spec, "gpt-5.6-terra")
        self.assertEqual(compiled.profile_id, "acme-agent")
        self.assertEqual(compiled.import_path, "acme.harbor.agent:AcmeAgent")
        self.assertEqual(
            compiled.model_name, "openai/gpt-5.6-terra-2026-08-01"
        )
        self.assertEqual(
            compiled.kwargs,
            {
                "features": ["tools", "atif"],
                "mode": "strict",
                "temperature": 0,
                "version": "2.4.1",
            },
        )
        self.assertEqual(
            compiled.env,
            {
                "ACME_API_KEY": "${ACME_API_KEY}",
                "ACME_BASE_URL": "${ACME_BASE_URL}",
            },
        )
        self.assertEqual(compiled.n_concurrent, 2)
        self.assertEqual(compiled.concurrency_group, "acme-api")
        self.assertEqual(
            compiled.extra_allowed_hosts, ("api.acme.test", "10.0.0.8")
        )

    def test_compiled_profiles_render_as_native_harbor_job_agents(self):
        task_set = self.tmp / "tasks"
        task = task_set / "example"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(
            'schema_version = "1.4"\n', encoding="utf-8"
        )
        (task / "instruction.md").write_text("# Example\n", encoding="utf-8")
        stock = compile_profile(
            load_profile(self._write("local-codex", self._stock())),
            "gpt-5.6-sol",
        )
        custom = compile_profile(
            load_profile(self._write("acme-agent", self._custom())),
            "gpt-5.6-sol",
        )

        artifact = hj.build_job_config(
            hj.HarborJobSpec(
                job_name="profile-contract",
                jobs_dir=self.tmp / "jobs",
                source=hj.LocalTaskSet(task_set),
                agent_profiles=(stock, custom),
                models=(),
                attempts=1,
                concurrency=hj.ConcurrencyPolicy(n_concurrent_trials=2),
                retry=hj.RetryPolicy(max_retries=0),
            )
        )

        agents = artifact.as_dict()["agents"]
        self.assertEqual(
            [(agent["import_path"], agent["model_name"]) for agent in agents],
            [
                (
                    "obench.harbor_agents.codex_profile:"
                    "OpenBenchCodexOAuthProfile",
                    "gpt-5.6-sol",
                ),
                (
                    "acme.harbor.agent:AcmeAgent",
                    "openai/gpt-5.6-sol-2026-08-01",
                ),
            ],
        )
        self.assertEqual(agents[1]["kwargs"]["version"], "2.4.1")

    def test_rejects_unknown_keys_for_each_kind(self):
        with self.assertRaisesRegex(ProfileSpecError, "unknown keys"):
            load_profile(
                self._write(
                    "local-codex",
                    self._stock() + 'credential_path = "/tmp/auth.json"\n',
                )
            )

    def test_custom_profile_requires_exact_distribution_identity(self):
        missing = self._custom().replace(
            'distribution = "acme-harbor-agent"\n', ""
        )
        with self.assertRaisesRegex(ProfileSpecError, "distribution"):
            load_profile(self._write("acme-agent", missing))

        invalid = self._custom().replace(
            'distribution = "acme-harbor-agent"',
            'distribution = "acme/harbor"',
        )
        with self.assertRaisesRegex(ProfileSpecError, "distribution"):
            load_profile(self._write("acme-agent", invalid))
        with self.assertRaisesRegex(ProfileSpecError, "unknown keys"):
            load_profile(
                self._write(
                    "acme-agent",
                    self._custom().replace(
                        'version = "2.4.1"',
                        'version = "2.4.1"\nlegacy_manifest = "candidate.toml"',
                    ),
                )
            )

    def test_rejects_literal_env_and_sensitive_or_non_json_kwargs(self):
        with self.assertRaisesRegex(ProfileSpecError, r"literal \$\{HOST_ENV\}"):
            load_profile(
                self._write(
                    "acme-agent",
                    self._custom().replace(
                        'ACME_API_KEY = "${ACME_API_KEY}"',
                        'ACME_API_KEY = "committed-secret"',
                    ),
                )
            )
        for key in ("api_key", "access_token", "session_cookie", "auth_path"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ProfileSpecError, "looks sensitive"):
                    load_profile(
                        self._write(
                            "acme-agent",
                            self._custom() + f'\n[kwargs.nested]\n{key} = "x"\n',
                        )
                    )
        with self.assertRaisesRegex(ProfileSpecError, "not JSON-safe"):
            load_profile(
                self._write(
                    "acme-agent",
                    self._custom() + "\n[kwargs.metadata]\ncreated = 2026-08-04\n",
                )
            )

    def test_rejects_floating_versions_and_invalid_import_paths(self):
        for version in ("latest", "main", "2.x", "^2.4.1", "2.4"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ProfileSpecError, "exact semantic"):
                    load_profile(
                        self._write(
                            "acme-agent",
                            self._custom().replace(
                                'version = "2.4.1"', f'version = "{version}"'
                            ),
                        )
                    )
        with self.assertRaisesRegex(ProfileSpecError, "module:Class"):
            load_profile(
                self._write(
                    "acme-agent",
                    self._custom().replace(
                        "acme.harbor.agent:AcmeAgent", "acme/agent.py:AcmeAgent"
                    ),
                )
            )

    def test_rejects_missing_or_ambiguous_model_mappings(self):
        spec = load_profile(self._write("acme-agent", self._custom()))
        with self.assertRaisesRegex(ProfileSpecError, "does not support"):
            compile_profile(spec, "gpt-5.5-medium")

        ambiguous = self._custom().replace(
            '"gpt-5.6-terra" = "openai/gpt-5.6-terra-2026-08-01"',
            '"gpt-5.6-terra" = "openai/gpt-5.6-sol-2026-08-01"',
        )
        with self.assertRaisesRegex(ProfileSpecError, "same Harbor model"):
            load_profile(self._write("acme-agent", ambiguous))

    def test_rejects_path_escape_and_symlink_components(self):
        outside = self.tmp / "outside.toml"
        outside.write_text(self._stock(), encoding="utf-8")
        with self.assertRaisesRegex(ProfileSpecError, "escapes"):
            load_profile(outside, project_root=self.tmp)

        real = self.tmp / "real.toml"
        real.write_text(self._stock(), encoding="utf-8")
        link = self.profiles / "local-codex.toml"
        link.symlink_to(real)
        with self.assertRaisesRegex(ProfileSpecError, "must not be a symlink"):
            load_profile(link)
        link.unlink()

        real_profiles = self.tmp / "real-profiles"
        real_profiles.mkdir()
        (self.tmp / ".openbench" / "profiles").rmdir()
        os.symlink(real_profiles, self.tmp / ".openbench" / "profiles")
        linked_file = real_profiles / "local-codex.toml"
        linked_file.write_text(self._stock(), encoding="utf-8")
        with self.assertRaisesRegex(ProfileSpecError, "directory must not be a symlink"):
            load_profile(linked_file, project_root=self.tmp)

    def test_rejects_filename_mismatch_and_duplicate_execution_identities(self):
        alias = self._write("alias", self._stock())
        with self.assertRaisesRegex(ProfileSpecError, "must match filename"):
            load_profile(alias)
        alias.unlink()

        self._write("codex-one", self._stock("codex-one"))
        self._write("codex-two", self._stock("codex-two"))
        with self.assertRaisesRegex(ProfileSpecError, "duplicate execution identities"):
            load_profile_registry(self.tmp)

    def test_rejects_ambiguous_concurrency_and_host_allowlist(self):
        without_limit = self._custom().replace("concurrency_limit = 2\n", "")
        with self.assertRaisesRegex(ProfileSpecError, "specified together"):
            load_profile(self._write("acme-agent", without_limit))

        for host in ("HTTPS://api.acme.test", "*.acme.test", "api.acme.test:443"):
            with self.subTest(host=host):
                invalid = self._custom().replace(
                    '"api.acme.test", "10.0.0.8"', f'"{host}"'
                )
                with self.assertRaises(ProfileSpecError):
                    load_profile(self._write("acme-agent", invalid))


if __name__ == "__main__":
    unittest.main()
