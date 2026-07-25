import dataclasses
import hashlib
import math
import tempfile
import textwrap
import unittest
from pathlib import Path

from obench.gateway_spec import (
    GatewaySpecError,
    canonical_digest,
    canonical_json,
    compile_route_plans,
    load_experiment,
    parse_experiment_toml,
)


def manifest(**replacements):
    values = {
        "track": "fixed_model_provider",
        "harness": "pi",
        "tasks": '["task-a", "task-b"]',
        "repetitions": "2",
        "windows": textwrap.dedent(
            """
            [[windows]]
            window_id = "morning"
            start = "2026-07-22T08:00:00Z"
            end = "2026-07-22T09:00:00Z"

            [[windows]]
            window_id = "evening"
            start = "2026-07-22T18:00:00-07:00"
            end = "2026-07-22T19:00:00-07:00"
            """
        ).strip(),
        "budget": textwrap.dedent(
            """
            [budget]
            timeout_s = 300
            max_calls = 8
            max_output_tokens = 16000
            usd_cap = 2.5
            """
        ).strip(),
        "direct_extra": "",
        "gateway_extra": (
            'gateway = "openrouter"\n'
            'direct_control_arm_id = "direct-openai"'
        ),
        "direct_auth": 'auth_env = "OPENAI_API_KEY"',
        "gateway_auth": 'auth_env = "OPENROUTER_API_KEY"',
        "direct_controls": textwrap.dedent(
            """
            fallback_enabled = false
            retry_count = 0
            cache_enabled = false
            """
        ).strip(),
        "gateway_controls": textwrap.dedent(
            """
            fallback_enabled = false
            retry_count = 0
            cache_enabled = false
            """
        ).strip(),
    }
    values.update(replacements)
    return textwrap.dedent(
        f"""
        schema_version = 2
        experiment_id = "gateway-bench-smoke"
        track = "{values['track']}"
        provider_prompt_mode = "provider_default"
        harness = "{values['harness']}"
        tasks = {values['tasks']}
        repetitions_per_window = {values['repetitions']}
        schedule_seed = 17
        execution_lane = "docker"
        allow_private_endpoint = false

        {values['windows']}

        {values['budget']}

        [[arms]]
        arm_id = "direct-openai"
        route_kind = "direct"
        endpoint = "https://api.openai.com/v1/chat/completions"
        protocol = "openai_chat"
        baseline = true
        canonical_model = "openai/gpt-test-2026-07-01"
        requested_model = "gpt-test-2026-07-01"
        requested_provider = "openai"
        allowed_models = ["gpt-test-2026-07-01"]
        allowed_providers = ["openai"]
        {values['direct_controls']}
        {values['direct_auth']}
        {values['direct_extra']}

        [arms.sampling]
        temperature = 0.0
        top_p = 1.0
        seed = 1234

        [[arms]]
        arm_id = "via-openrouter"
        route_kind = "gateway"
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        protocol = "openai_chat"
        baseline = false
        canonical_model = "openai/gpt-test-2026-07-01"
        requested_model = "gpt-test-2026-07-01"
        requested_provider = "openai"
        allowed_models = ["gpt-test-2026-07-01"]
        allowed_providers = ["openai"]
        {values['gateway_controls']}
        {values['gateway_auth']}
        {values['gateway_extra']}

        [arms.sampling]
        temperature = 0.0
        top_p = 1.0
        seed = 1234
        """
    )


class GatewayExperimentTests(unittest.TestCase):
    def test_loads_frozen_normalized_experiment(self):
        spec = parse_experiment_toml(manifest())

        self.assertEqual(spec.track, "fixed_model_provider")
        self.assertEqual(spec.tasks, ("task-a", "task-b"))
        self.assertEqual(spec.arms[1].direct_control_arm_id, "direct-openai")
        self.assertEqual(spec.budget.usd_cap, "2.5")
        self.assertTrue(spec.windows[0].start.endswith("Z"))
        self.assertTrue(spec.windows[1].start.endswith("Z"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.track = "model_router"

    def test_model_match_defaults_exact_and_rolling_alias_is_normative(self):
        defaulted = parse_experiment_toml(manifest())
        rolling = parse_experiment_toml(
            manifest().replace(
                'track = "fixed_model_provider"',
                'track = "fixed_model_provider"\nmodel_match = "rolling_alias"',
            )
        )

        self.assertEqual(defaulted.model_match, "exact_revision")
        self.assertEqual(rolling.model_match, "rolling_alias")
        self.assertNotEqual(defaulted.digest, rolling.digest)
        with self.assertRaisesRegex(GatewaySpecError, "model_match"):
            parse_experiment_toml(
                manifest().replace(
                    'track = "fixed_model_provider"',
                    'track = "fixed_model_provider"\nmodel_match = "alias"',
                )
            )

    def test_provider_prompt_mode_is_required_and_isolation_is_responses_only(self):
        natural = parse_experiment_toml(manifest())
        self.assertEqual(natural.provider_prompt_mode, "provider_default")

        responses = (
            manifest()
            .replace(
                'provider_prompt_mode = "provider_default"',
                'provider_prompt_mode = "isolated_per_call_v1"',
            )
            .replace("/chat/completions", "/responses")
            .replace('protocol = "openai_chat"', 'protocol = "openai_responses"')
        )
        cold = parse_experiment_toml(responses)
        self.assertEqual(cold.provider_prompt_mode, "isolated_per_call_v1")
        self.assertNotEqual(natural.digest, cold.digest)

        non_openai_direct = responses.replace(
            'requested_provider = "openai"',
            'requested_provider = "anthropic"',
        ).replace(
            'allowed_providers = ["openai"]',
            'allowed_providers = ["anthropic"]',
        )
        with self.assertRaisesRegex(GatewaySpecError, "direct OpenAI"):
            parse_experiment_toml(non_openai_direct)

        non_openai_endpoint = responses.replace(
            "https://api.openai.com",
            "https://api.example.com",
            1,
        )
        with self.assertRaisesRegex(GatewaySpecError, "direct OpenAI"):
            parse_experiment_toml(non_openai_endpoint)

        with self.assertRaisesRegex(GatewaySpecError, "requires openai_responses"):
            parse_experiment_toml(
                manifest().replace(
                    'provider_prompt_mode = "provider_default"',
                    'provider_prompt_mode = "isolated_per_call_v1"',
                )
            )
        with self.assertRaisesRegex(GatewaySpecError, "provider_prompt_mode"):
            parse_experiment_toml(
                manifest().replace(
                    'provider_prompt_mode = "provider_default"',
                    'provider_prompt_mode = "disabled"',
                )
            )
        with self.assertRaisesRegex(GatewaySpecError, "provider_prompt_mode"):
            parse_experiment_toml(
                manifest().replace(
                    'provider_prompt_mode = "provider_default"\n',
                    "",
                )
            )

    def test_rejects_router_track_and_auto_only_fields(self):
        with self.assertRaisesRegex(GatewaySpecError, "track must be"):
            parse_experiment_toml(
                manifest().replace(
                    'track = "fixed_model_provider"',
                    'track = "model_router"',
                )
            )
        with self.assertRaisesRegex(GatewaySpecError, "unknown field: router_mode"):
            parse_experiment_toml(
                manifest().replace(
                    'route_kind = "gateway"',
                    'route_kind = "gateway"\nrouter_mode = "auto"',
                )
            )

    def test_load_file_matches_text_and_digest_ignores_toml_formatting(self):
        text = manifest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "experiment.toml")
            path.write_text(text, encoding="utf-8")
            from_file = load_experiment(path)

        from_text = parse_experiment_toml("\n# comment\n" + text)
        self.assertEqual(from_file, from_text)
        self.assertEqual(from_file.digest, from_text.digest)
        self.assertEqual(
            from_file.digest,
            hashlib.sha256(from_file.canonical_json.encode("utf-8")).hexdigest(),
        )

    def test_canonical_json_is_sorted_compact_and_rejects_non_finite_values(self):
        self.assertEqual(canonical_json({"z": 1, "a": [True, None]}),
                         '{"a":[true,null],"z":1}')
        self.assertEqual(canonical_digest({"b": 2, "a": 1}),
                         canonical_digest({"a": 1, "b": 2}))
        with self.assertRaises(GatewaySpecError):
            canonical_json({"bad": math.inf})

    def test_route_plan_is_sanitized_and_secret_plan_is_memory_only(self):
        spec = parse_experiment_toml(manifest())
        plans, secrets = compile_route_plans(
            spec,
            environ={
                "OPENAI_API_KEY": "direct-secret-value",
                "OPENROUTER_API_KEY": "gateway-secret-value",
            },
            admitted_auth_envs={"OPENAI_API_KEY", "OPENROUTER_API_KEY"},
        )

        persisted = canonical_json([plan.to_dict() for plan in plans])
        self.assertIn("OPENAI_API_KEY", persisted)
        self.assertNotIn("direct-secret-value", persisted)
        self.assertNotIn("gateway-secret-value", persisted)
        self.assertEqual(plans[0].provider_prompt_mode, "provider_default")
        self.assertEqual(secrets.value_for("direct-openai"), "direct-secret-value")
        self.assertNotIn("direct-secret-value", repr(secrets))
        with self.assertRaises(TypeError):
            canonical_json(secrets)

    def test_secret_use_requires_explicit_admission_and_present_value(self):
        spec = parse_experiment_toml(manifest())
        with self.assertRaisesRegex(GatewaySpecError, "not explicitly admitted"):
            compile_route_plans(
                spec,
                environ={"OPENAI_API_KEY": "x", "OPENROUTER_API_KEY": "y"},
                admitted_auth_envs={"OPENAI_API_KEY"},
            )
        with self.assertRaisesRegex(GatewaySpecError, "missing or empty"):
            compile_route_plans(
                spec,
                environ={"OPENAI_API_KEY": "x"},
                admitted_auth_envs={"OPENAI_API_KEY", "OPENROUTER_API_KEY"},
            )

    def test_auth_accepts_environment_variable_names_never_values(self):
        with self.assertRaisesRegex(GatewaySpecError, "auth_env"):
            parse_experiment_toml(
                manifest(gateway_auth='auth_env = "sk-live-secret"')
            )
        with self.assertRaisesRegex(GatewaySpecError, "unknown field"):
            parse_experiment_toml(
                manifest(gateway_extra='direct_control_arm_id = "direct-openai"\nauth_value = "secret"')
            )

    def test_rejects_unknown_keys_at_every_level(self):
        cases = (
            manifest().replace(
                "schema_version = 2", "schema_version = 2\nunexpected = true", 1),
            manifest(budget=manifest_budget() + "\nextra = 1"),
            manifest(gateway_extra='direct_control_arm_id = "direct-openai"\nextra = 1'),
            manifest(gateway_extra=(
                'gateway_id = "strict-tax"\n'
                'direct_control_arm_id = "direct-openai"'
            )),
            manifest(windows=manifest_windows().replace(
                'window_id = "morning"', 'window_id = "morning"\nextra = 1', 1)),
            manifest().replace("temperature = 0.0", "temperature = 0.0\nextra = 1", 1),
        )
        for text in cases:
            with self.subTest(text=text[-80:]):
                with self.assertRaisesRegex(GatewaySpecError, "unknown field"):
                    parse_experiment_toml(text)

    def test_rejects_invalid_track_harness_tasks_windows_and_budget(self):
        cases = (
            (manifest(track="provider_router"), "track"),
            (manifest(harness="codex"), "harness"),
            (manifest(tasks='["task-a", "task-a"]'), "tasks"),
            (manifest(repetitions="0"), "repetitions_per_window"),
            (manifest(windows=overlapping_windows()), "overlap"),
            (manifest(budget=manifest_budget().replace("max_calls = 8", "max_calls = 0")),
             "max_calls"),
            (manifest(budget=manifest_budget().replace("usd_cap = 2.5", "usd_cap = -1")),
             "usd_cap"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(GatewaySpecError, message):
                    parse_experiment_toml(text)

    def test_gateway_may_use_endpoint_specific_wire_model_id(self):
        text = manifest().replace(
            'requested_model = "gpt-test-2026-07-01"',
            'requested_model = "openai/gpt-test-2026-07-01"',
            1,
        ).replace(
            'allowed_models = ["gpt-test-2026-07-01"]',
            'allowed_models = ["openai/gpt-test-2026-07-01"]',
            1,
        )
        spec = parse_experiment_toml(text)
        self.assertEqual(spec.arms[0].canonical_model, spec.arms[1].canonical_model)
        self.assertNotEqual(spec.arms[0].requested_model, spec.arms[1].requested_model)

    def test_fixed_model_provider_requires_direct_control_and_equal_fixed_conditions(self):
        cases = (
            (manifest(gateway_extra=""), "direct_control_arm_id"),
            (manifest(direct_extra='direct_control_arm_id = "via-openrouter"'),
             "direct arm"),
            (manifest(gateway_extra=(
                'gateway = "openrouter"\n'
                'direct_control_arm_id = "missing"'
            )),
             "unknown direct control"),
            (manifest(gateway_controls=manifest_controls().replace(
                "fallback_enabled = false", "fallback_enabled = true")), "fallback"),
            (manifest(gateway_controls=manifest_controls().replace(
                "retry_count = 0", "retry_count = 1")), "retry_count"),
            (manifest(gateway_controls=manifest_controls().replace(
                "cache_enabled = false", "cache_enabled = true")), "cache"),
            (manifest().replace(
                'canonical_model = "openai/gpt-test-2026-07-01"',
                'canonical_model = "openai/other-model"', 1), "canonical_model"),
            (manifest().replace(
                'allowed_providers = ["openai"]',
                'allowed_providers = ["openai", "other"]', 1), "only requested_provider"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(GatewaySpecError, message):
                    parse_experiment_toml(text)

    def test_rejects_non_https_and_private_endpoint_without_private_admission(self):
        with self.assertRaisesRegex(GatewaySpecError, "HTTPS"):
            parse_experiment_toml(manifest().replace("https://api.openai.com", "http://api.openai.com"))
        with self.assertRaisesRegex(GatewaySpecError, "allow_private_endpoint"):
            parse_experiment_toml(manifest().replace("api.openai.com", "127.0.0.1"))

    def test_gateway_profiles_are_explicit_and_concentrate_responses_is_admitted(self):
        with self.assertRaisesRegex(GatewaySpecError, "requires gateway"):
            parse_experiment_toml(manifest(gateway_extra=(
                'direct_control_arm_id = "direct-openai"'
            )))
        with self.assertRaisesRegex(GatewaySpecError, "must not declare gateway"):
            parse_experiment_toml(manifest(direct_extra='gateway = "openrouter"'))
        concentrate = manifest(
            gateway_auth='auth_env = "CONCENTRATE_API_KEY"',
            gateway_extra=(
                'gateway = "concentrate"\n'
                'direct_control_arm_id = "direct-openai"'
            ),
        ).replace(
            'protocol = "openai_chat"', 'protocol = "openai_responses"'
        ).replace(
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1/responses",
        ).replace(
            "https://openrouter.ai/api/v1/chat/completions",
            "https://api.concentrate.ai/v1/responses",
        )
        concentrate = concentrate.rsplit(
            'requested_model = "gpt-test-2026-07-01"', 1
        )
        concentrate = (
            'requested_model = "openai/gpt-test-2026-07-01"'
        ).join(concentrate)
        concentrate = concentrate.rsplit(
            'allowed_models = ["gpt-test-2026-07-01"]', 1
        )
        concentrate = (
            'allowed_models = ["openai/gpt-test-2026-07-01"]'
        ).join(concentrate)
        parsed = parse_experiment_toml(concentrate)
        self.assertEqual(parsed.arms[1].gateway, "concentrate")

        invalid = (
            (
                concentrate.replace(
                    "https://api.concentrate.ai/v1/responses",
                    "https://api.concentrate.ai/v1/chat/completions",
                ),
                "endpoint path must end with /responses",
            ),
            (
                concentrate.replace(
                    'requested_model = "openai/gpt-test-2026-07-01"',
                    'requested_model = "gpt-test-2026-07-01"',
                    1,
                ).replace(
                    'allowed_models = ["openai/gpt-test-2026-07-01"]',
                    'allowed_models = ["gpt-test-2026-07-01"]',
                    1,
                ),
                "provider-qualified",
            ),
        )
        for text, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(GatewaySpecError, message):
                    parse_experiment_toml(text)

    def test_vercel_is_admitted_and_cloudflare_requires_logs_verification(self):
        vercel = manifest(
            gateway_auth='auth_env = "AI_GATEWAY_API_KEY"',
            gateway_extra=(
                'gateway = "vercel"\n'
                'direct_control_arm_id = "direct-openai"'
            ),
        ).replace(
            "https://openrouter.ai/api/v1/chat/completions",
            "https://ai-gateway.vercel.sh/v1/chat/completions",
        )
        vercel = vercel.rsplit(
            'requested_model = "gpt-test-2026-07-01"',
            1,
        )
        vercel = (
            'requested_model = "openai/gpt-test-2026-07-01"'
        ).join(vercel)
        vercel = vercel.rsplit(
            'allowed_models = ["gpt-test-2026-07-01"]',
            1,
        )
        vercel = (
            'allowed_models = ["openai/gpt-test-2026-07-01"]'
        ).join(vercel)
        self.assertEqual(parse_experiment_toml(vercel).arms[1].gateway, "vercel")

        with self.assertRaisesRegex(
            GatewaySpecError,
            "metadata-only Logs API verification is required",
        ):
            parse_experiment_toml(manifest(
                gateway_auth='auth_env = "CLOUDFLARE_API_TOKEN"',
                gateway_extra=(
                    'gateway = "cloudflare"\n'
                    'direct_control_arm_id = "direct-openai"'
                ),
            ))

    def test_responses_protocol_requires_responses_endpoints(self):
        responses = manifest().replace(
            "/chat/completions", "/responses"
        ).replace(
            'protocol = "openai_chat"', 'protocol = "openai_responses"'
        )
        parsed = parse_experiment_toml(responses)
        self.assertEqual(
            {arm.protocol for arm in parsed.arms},
            {"openai_responses"},
        )
        with self.assertRaisesRegex(
            GatewaySpecError,
            "endpoint path must end with /responses",
        ):
            parse_experiment_toml(
                responses.replace(
                    "https://api.openai.com/v1/responses",
                    "https://api.openai.com/v1/chat/completions",
                )
            )

    def test_private_literal_endpoint_must_match_declared_allowlist(self):
        private = manifest().replace(
            "allow_private_endpoint = false",
            (
                'allow_private_endpoint = true\n'
                'private_host_allowlist = ["openrouter.ai"]\n'
                'private_cidr_allowlist = ["10.0.0.0/8"]'
            ),
        ).replace("api.openai.com", "127.0.0.1")
        with self.assertRaisesRegex(GatewaySpecError, "not covered"):
            parse_experiment_toml(private)

        admitted = private.replace("10.0.0.0/8", "127.0.0.0/8")
        self.assertEqual(
            parse_experiment_toml(admitted).arms[0].endpoint,
            "https://127.0.0.1/v1/chat/completions",
        )

    def test_private_host_allowlist_is_validated_and_normalized(self):
        valid = manifest().replace(
            "allow_private_endpoint = false",
            (
                'allow_private_endpoint = true\n'
                'private_host_allowlist = '
                '["Router.Internal.", "api.openai.com", "openrouter.ai"]'
            ),
        )
        self.assertEqual(
            parse_experiment_toml(valid).private_host_allowlist,
            ("router.internal", "api.openai.com", "openrouter.ai"),
        )
        with self.assertRaisesRegex(GatewaySpecError, "bare DNS hostname"):
            parse_experiment_toml(valid.replace("Router.Internal.", "bad host:443"))

        unlisted = valid.replace("api.openai.com", "attacker.example.com", 1)
        with self.assertRaisesRegex(GatewaySpecError, "explicit endpoint allowlist"):
            parse_experiment_toml(unlisted)

    def test_window_timestamps_require_rfc3339_not_broader_iso8601(self):
        invalid = manifest().replace(
            "2026-07-22T08:00:00Z", "2026-07-22 08:00:00+00:00"
        )
        with self.assertRaisesRegex(GatewaySpecError, "RFC3339"):
            parse_experiment_toml(invalid)

    def test_compile_revalidates_manually_constructed_experiment(self):
        spec = parse_experiment_toml(manifest())
        invalid_arm = dataclasses.replace(spec.arms[0], endpoint="http://127.0.0.1")
        invalid_spec = dataclasses.replace(spec, arms=(invalid_arm, spec.arms[1]))

        with self.assertRaisesRegex(GatewaySpecError, "HTTPS"):
            compile_route_plans(
                invalid_spec,
                environ={"OPENAI_API_KEY": "x", "OPENROUTER_API_KEY": "y"},
                admitted_auth_envs={"OPENAI_API_KEY", "OPENROUTER_API_KEY"},
            )


def manifest_budget():
    return textwrap.dedent(
        """
        [budget]
        timeout_s = 300
        max_calls = 8
        max_output_tokens = 16000
        usd_cap = 2.5
        """
    ).strip()


def manifest_controls():
    return textwrap.dedent(
        """
        fallback_enabled = false
        retry_count = 0
        cache_enabled = false
        """
    ).strip()


def manifest_windows():
    return textwrap.dedent(
        """
        [[windows]]
        window_id = "morning"
        start = "2026-07-22T08:00:00Z"
        end = "2026-07-22T09:00:00Z"

        [[windows]]
        window_id = "evening"
        start = "2026-07-22T18:00:00-07:00"
        end = "2026-07-22T19:00:00-07:00"
        """
    ).strip()


def overlapping_windows():
    return manifest_windows().replace(
        'start = "2026-07-22T18:00:00-07:00"',
        'start = "2026-07-22T08:30:00Z"',
    ).replace(
        'end = "2026-07-22T19:00:00-07:00"',
        'end = "2026-07-22T09:30:00Z"',
    )


if __name__ == "__main__":
    unittest.main()
