"""Deterministic contract tests for pinned Harbor harness profiles."""

from __future__ import annotations

import json
import unittest

from obench.harbor_profiles import (
    HARBOR_VERSION,
    HarborProfileError,
    resolve_harbor_profile,
    supported_harbor_matrix,
)


class HarborProfileTests(unittest.TestCase):
    def test_compatibility_matrix_is_complete_and_deterministic(self):
        expected = tuple(
            (harness, model)
            for harness in ("codex", "opencode", "pi")
            for model in (
                "gpt-5.5-medium",
                "gpt-5.6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
            )
        )
        self.assertEqual(HARBOR_VERSION, "0.20.0")
        self.assertEqual(supported_harbor_matrix(), expected)

    def test_resolves_exact_agent_identity_versions_models_and_flags(self):
        expected = {
            "codex": (
                (
                    "obench.harbor_agents.codex_profile:"
                    "OpenBenchCodexOAuthProfile"
                ),
                "0.144.5",
                "gpt-5.6-sol",
                {"reasoning_effort": "medium"},
            ),
            "pi": (
                "obench.harbor_agents.pi:OpenBenchPiOAuth",
                "0.80.10",
                "openai-codex/gpt-5.6-sol",
                {"thinking": "medium"},
            ),
            "opencode": (
                "obench.harbor_agents.opencode:OpenBenchOpenCodeOAuth",
                "1.18.3",
                "openai/gpt-5.6-sol",
                {"variant": "medium"},
            ),
        }
        for harness, values in expected.items():
            with self.subTest(harness=harness):
                profile = resolve_harbor_profile(harness, "gpt-5.6-sol")
                agent_import, version, model_name, flags = values
                self.assertEqual(profile.semantic_name, harness)
                self.assertEqual(profile.agent_import_path, agent_import)
                self.assertEqual(profile.cli_version, version)
                self.assertEqual(profile.harbor_model_name, model_name)
                kwargs = profile.agent_kwargs()
                self.assertEqual(kwargs.pop("version"), version)
                config = kwargs.pop("config", None)
                self.assertEqual(kwargs, flags)
                if harness == "codex":
                    self.assertEqual(config["service_tier"], "default")
                    self.assertEqual(
                        config["features"],
                        {"apps": False, "plugins": False, "multi_agent": False},
                    )
                else:
                    self.assertIsNone(config)

    def test_codex_55_does_not_invent_service_tier_override(self):
        kwargs = resolve_harbor_profile(
            "codex", "gpt-5.5-medium"
        ).agent_kwargs()
        self.assertNotIn("service_tier", kwargs["config"])

    def test_agent_config_is_path_only_and_returns_fresh_nested_data(self):
        profile = resolve_harbor_profile("codex", "gpt-5.6-sol")
        config = profile.agent_config(
            auth_json_path="/private/stage/auth.json",
            auth_return_path="/private/stage/auth-return.json",
        )
        self.assertEqual(
            config,
            {
                "name": None,
                "import_path": (
                    "obench.harbor_agents.codex_profile:"
                    "OpenBenchCodexOAuthProfile"
                ),
                "model_name": "gpt-5.6-sol",
                "n_concurrent": 1,
                "concurrency_group": "openbench-oauth-codex",
                "kwargs": {
                    "version": "0.144.5",
                    "reasoning_effort": "medium",
                    "config": {
                        "features": {
                            "apps": False,
                            "plugins": False,
                            "multi_agent": False,
                        },
                        "service_tier": "default",
                    },
                },
                "env": {
                    "CODEX_AUTH_JSON_PATH": "/private/stage/auth.json",
                    "OPENBENCH_CODEX_AUTH_RETURN_PATH": (
                        "/private/stage/auth-return.json"
                    ),
                },
            },
        )
        config["kwargs"]["config"]["features"]["apps"] = True
        self.assertFalse(
            profile.agent_kwargs()["config"]["features"]["apps"]
        )
        self.assertNotIn("token", json.dumps(config).lower())

    def test_auth_contracts_require_lease_and_persist_back(self):
        expected_sources = {
            "codex": ("~/.codex/auth.json",),
            "pi": ("~/.pi/agent/auth.json",),
            "opencode": (
                "~/.local/share/opencode/auth.json",
                "~/.opencode/data/auth.json",
            ),
        }
        for harness, sources in expected_sources.items():
            profile = resolve_harbor_profile(harness, "gpt-5.5-medium")
            with self.subTest(harness=harness):
                self.assertEqual(profile.auth.strategy, "oauth")
                self.assertEqual(profile.auth.source_candidates, sources)
                self.assertTrue(profile.auth.persist_back)
                self.assertTrue(profile.auth.lease_required)
                self.assertEqual(profile.auth.max_concurrent_uses, 1)
                self.assertEqual(
                    profile.auth.concurrency_group,
                    f"openbench-oauth-{harness}",
                )

    def test_proxy_contract_and_injection_are_explicit(self):
        codex = resolve_harbor_profile(
            "codex",
            "gpt-5.6-sol",
            proxy_base_url="http://127.0.0.1:4100/cell/token/codex/backend-api/codex/",
        )
        self.assertEqual(codex.proxy.route, "codex/backend-api/codex")
        self.assertEqual(
            dict(codex.agent_env)["OPENAI_BASE_URL"],
            "http://127.0.0.1:4100/cell/token/codex/backend-api/codex",
        )

        pi = resolve_harbor_profile(
            "pi",
            "gpt-5.6-sol",
            proxy_base_url="http://127.0.0.1:4100/cell/token/codex/backend-api",
        )
        self.assertEqual(pi.proxy.route, "codex/backend-api")
        self.assertEqual(
            dict(pi.agent_env)["OPENBENCH_PI_BASE_URL"],
            "http://127.0.0.1:4100/cell/token/codex/backend-api",
        )
        with self.assertRaisesRegex(
            HarborProfileError, "opencode.*unsupported"
        ):
            resolve_harbor_profile(
                "opencode",
                "gpt-5.6-sol",
                proxy_base_url=(
                    "http://127.0.0.1:4100/cell/token/codex/backend-api"
                ),
            )

    def test_unsupported_inputs_fail_closed(self):
        cases = (
            (("claude", "gpt-5.6-sol"), {}, "unsupported Harbor harness"),
            (("pi", "claude-opus-4-8"), {}, "unsupported pi Harbor model"),
            (("codex", "gpt-5.6-sol"), {"auth_strategy": "api-key"}, "oauth"),
            (
                ("pi", "gpt-5.6-sol"),
                {"proxy_base_url": "127.0.0.1:4100"},
                "absolute HTTP",
            ),
        )
        for args, kwargs, message in cases:
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaisesRegex(HarborProfileError, message):
                    resolve_harbor_profile(*args, **kwargs)

    def test_agent_config_rejects_unsafe_path_contracts(self):
        profile = resolve_harbor_profile("pi", "gpt-5.6-sol")
        with self.assertRaisesRegex(HarborProfileError, "absolute"):
            profile.agent_config(
                auth_json_path="relative/auth.json",
                auth_return_path="/tmp/auth-return.json",
            )
        with self.assertRaisesRegex(HarborProfileError, "distinct"):
            profile.agent_config(
                auth_json_path="/tmp/auth.json",
                auth_return_path="/tmp/auth.json",
            )


if __name__ == "__main__":
    unittest.main()
