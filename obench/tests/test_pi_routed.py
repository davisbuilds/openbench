import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench import entry
from obench.adapters import pi
from obench.router_spec import (
    RoutePlan,
    RouterSpecError,
    Sampling,
    compile_route_plans,
    parse_experiment_toml,
)
from obench.tests.test_router_spec import model_router_manifest


def plan_dict(**updates):
    plan = RoutePlan(
        schema_version=1,
        experiment_digest="a" * 64,
        arm_digest="b" * 64,
        arm_id="gateway",
        route_kind="gateway",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        protocol="openai_chat",
        canonical_model="provider-a/model-new",
        requested_model="vendor/model-new",
        requested_provider="provider-a",
        allowed_models=("vendor/model-new",),
        allowed_providers=("provider-a",),
        fallback_enabled=False,
        retry_count=0,
        cache_enabled=False,
        auth_env="ROUTER_API_KEY",
        sampling=Sampling(0.25, 0.9, 42),
        private_router=False,
        private_host_allowlist=(),
        private_cidr_allowlist=(),
    ).to_dict()
    plan.update(updates)
    return plan


class PiRoutedTests(unittest.TestCase):
    def _write_plan(self, directory, data=None):
        path = Path(directory, "route-plan.json")
        path.write_text(json.dumps(data or plan_dict()), encoding="utf-8")
        return str(path)

    def test_capabilities_are_strict_v2(self):
        self.assertEqual(pi.ADAPTER_API_VERSION, 2)
        self.assertEqual(pi.ROUTED_CAPABILITIES, {
            "protocols": ["openai_chat"],
            "execution_lanes": ["local", "docker"],
            "streaming": True,
            "dynamic_model_ids": True,
            "route_plan_transport": "sanitized_file",
        })

    def test_routed_launch_uses_digest_route_and_sanitized_env(self):
        captured = {}

        def fake_run(cmd, cwd, timeout_s, env):
            captured.update(cmd=cmd, cwd=cwd, timeout=timeout_s, env=env)
            captured["extension"] = Path(cmd[cmd.index("-e") + 1]).read_text()
            return "", "", 0, False

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(tmp)
            env = {
                "OPENBENCH_PROXY": "1",
                "OPENBENCH_PROXY_BASE_URL": "http://127.0.0.1:8123",
                "OPENBENCH_PROXY_CELL_TOKEN": "cell-1",
                "PATH": os.environ.get("PATH", ""),
                "OPENAI_API_KEY": "sk-inherited",
                "ROUTER_API_KEY": "router-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(pi, "_run_streaming", side_effect=fake_run):
                result = pi.run_routed("fix it", tmp, path, 17)

        self.assertTrue(result["completed"])
        self.assertIn("vendor/model-new", captured["cmd"])
        self.assertEqual(captured["cwd"], tmp)
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("ROUTER_API_KEY", captured["env"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", captured["env"])
        extension = captured["extension"]
        self.assertIn(
            "http://127.0.0.1:8123/cell/cell-1/route/" + "b" * 64,
            extension,
        )
        self.assertNotIn("openRouterRouting", extension)
        self.assertNotIn('"provider"', extension)
        self.assertNotIn('"temperature"', extension)
        self.assertNotIn('"top_p"', extension)
        self.assertNotIn('"seed"', extension)
        self.assertIn('maxTokensField: "max_tokens"', extension)
        self.assertIn("contextWindow: 128000, maxTokens: 16384", extension)
        self.assertNotIn("router-secret", extension)
        self.assertNotIn("sk-inherited", extension)

    def test_malformed_and_unsupported_plans_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            malformed = self._write_plan(tmp, {**plan_dict(), "secret": "no"})
            unsupported = Path(tmp, "unsupported.json")
            unsupported.write_text(json.dumps({
                **plan_dict(), "protocol": "anthropic_messages",
            }), encoding="utf-8")
            for path in (malformed, unsupported):
                with self.subTest(path=path), \
                        mock.patch.object(pi, "_run_streaming") as launch:
                    with self.assertRaises(RouterSpecError):
                        pi.run_routed("x", tmp, path, 1)
                    launch.assert_not_called()

    def test_loads_compiled_model_router_auto_plan(self):
        experiment = parse_experiment_toml(model_router_manifest())
        plans, _ = compile_route_plans(
            experiment,
            environ={"OPENROUTER_API_KEY": "secret"},
            admitted_auth_envs={"OPENROUTER_API_KEY"},
        )
        auto = plans[0]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "route-plan.json")
            path.write_text(auto.canonical_json + "\n", encoding="utf-8")
            loaded = pi._load_route_plan(path)

        self.assertEqual(loaded.requested_model, "openrouter/auto-beta")
        self.assertEqual(loaded.allowed_models, ("openai/gpt-fixed",))
        self.assertEqual(loaded.allowed_providers, ("openai",))
        self.assertTrue(loaded.fallback_enabled)

    def test_rejects_invalid_auto_and_preserves_fixed_pool_membership(self):
        cases = (
            (
                {
                    **plan_dict(),
                    "fallback_enabled": True,
                    "requested_model": "openrouter/not-auto",
                },
                "openrouter/auto-beta",
            ),
            (
                {
                    **plan_dict(),
                    "fallback_enabled": True,
                    "requested_model": "openrouter/auto-beta",
                    "requested_provider": "not-openrouter",
                },
                "provider 'openrouter'",
            ),
            (
                {
                    **plan_dict(),
                    "fallback_enabled": True,
                    "requested_model": "openrouter/auto-beta",
                    "requested_provider": "openrouter",
                    "allowed_models": [],
                },
                "allowed_models must be a non-empty array",
            ),
            (
                {
                    **plan_dict(),
                    "fallback_enabled": True,
                    "requested_model": "openrouter/auto-beta",
                    "requested_provider": "openrouter",
                    "allowed_providers": [],
                },
                "allowed_providers must be a non-empty array",
            ),
            (
                {**plan_dict(), "requested_model": "vendor/not-allowed"},
                "allowed_models must contain requested_model",
            ),
            (
                {**plan_dict(), "requested_provider": "not-allowed"},
                "allowed_providers must contain requested_provider",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, (data, message) in enumerate(cases):
                with self.subTest(message=message):
                    path = Path(tmp, f"route-plan-{index}.json")
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(RouterSpecError, message):
                        pi._load_route_plan(path)

    def test_entry_rejects_incompatible_capabilities_before_dispatch(self):
        adapter = mock.Mock(
            ADAPTER_API_VERSION=1,
            ROUTED_CAPABILITIES=pi.ROUTED_CAPABILITIES,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(tmp)
            with self.assertRaisesRegex(ValueError, "ADAPTER_API_VERSION"):
                entry._validate_routed_adapter(adapter, path)
            adapter.run_routed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
