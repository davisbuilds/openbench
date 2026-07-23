import dataclasses
import hashlib
import math
import tempfile
import textwrap
import unittest
from pathlib import Path

from obench.router_spec import (
    RouterSpecError,
    canonical_digest,
    canonical_json,
    compile_route_plans,
    load_experiment,
    parse_experiment_toml,
)


def manifest(**replacements):
    values = {
        "track": "gateway_tax",
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
        schema_version = 1
        experiment_id = "gateway-tax-smoke"
        track = "{values['track']}"
        harness = "{values['harness']}"
        tasks = {values['tasks']}
        repetitions_per_window = {values['repetitions']}
        schedule_seed = 17
        execution_lane = "docker"
        private_router = false

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


def model_router_manifest(**replacements):
    values = {
        "model_match": "exact_revision",
        "auto_models": '["openai/gpt-fixed"]',
        "auto_providers": '["openai"]',
        "auto_model": "openrouter/auto-beta",
        "auto_provider": "openrouter",
        "auto_tradeoff": "6",
        "auto_fallback": "true",
        "auto_control": 'fixed_control_arm_id = "fixed-openai"',
        "fixed_mode": "fixed",
        "fixed_baseline": "true",
        "fixed_gateway": 'gateway = "openrouter"',
    }
    values.update(replacements)
    return textwrap.dedent(
        f"""
        schema_version = 1
        experiment_id = "model-router-smoke"
        track = "model_router"
        model_match = "{values['model_match']}"
        harness = "pi"
        tasks = ["task-a"]
        repetitions_per_window = 1
        schedule_seed = 17
        execution_lane = "local"
        private_router = false

        [[windows]]
        window_id = "window"
        start = "2026-07-22T08:00:00Z"
        end = "2026-07-22T09:00:00Z"

        [budget]
        timeout_s = 300
        max_calls = 8
        max_output_tokens = 16000
        usd_cap = 2.5

        [[arms]]
        arm_id = "auto"
        route_kind = "gateway"
        router_mode = "auto"
        gateway = "openrouter"
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        protocol = "openai_chat"
        baseline = false
        canonical_model = "openrouter/auto-beta"
        requested_model = "{values['auto_model']}"
        requested_provider = "{values['auto_provider']}"
        allowed_models = {values['auto_models']}
        allowed_providers = {values['auto_providers']}
        fallback_enabled = {values['auto_fallback']}
        retry_count = 0
        cache_enabled = false
        cost_quality_tradeoff = {values['auto_tradeoff']}
        {values['auto_control']}
        auth_env = "OPENROUTER_API_KEY"

        [arms.sampling]
        temperature = 0.0
        top_p = 1.0
        seed = 1234

        [[arms]]
        arm_id = "fixed-openai"
        route_kind = "gateway"
        router_mode = "{values['fixed_mode']}"
        {values['fixed_gateway']}
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        protocol = "openai_chat"
        baseline = {values['fixed_baseline']}
        canonical_model = "openai/gpt-fixed"
        requested_model = "openai/gpt-fixed"
        requested_provider = "openai"
        allowed_models = ["openai/gpt-fixed"]
        allowed_providers = ["openai"]
        fallback_enabled = false
        retry_count = 0
        cache_enabled = false
        auth_env = "OPENROUTER_API_KEY"

        [arms.sampling]
        temperature = 0.0
        top_p = 1.0
        seed = 1234
        """
    )


class RouterExperimentTests(unittest.TestCase):
    def test_loads_frozen_normalized_experiment(self):
        spec = parse_experiment_toml(manifest())

        self.assertEqual(spec.track, "gateway_tax")
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
                'track = "gateway_tax"',
                'track = "gateway_tax"\nmodel_match = "rolling_alias"',
            )
        )

        self.assertEqual(defaulted.model_match, "exact_revision")
        self.assertEqual(rolling.model_match, "rolling_alias")
        self.assertNotEqual(defaulted.digest, rolling.digest)
        with self.assertRaisesRegex(RouterSpecError, "model_match"):
            parse_experiment_toml(
                manifest().replace(
                    'track = "gateway_tax"',
                    'track = "gateway_tax"\nmodel_match = "alias"',
                )
            )

    def test_model_router_accepts_one_auto_arm_and_openrouter_fixed_control(self):
        spec = parse_experiment_toml(model_router_manifest())

        self.assertEqual(spec.track, "model_router")
        self.assertEqual(spec.arms[0].router_mode, "auto")
        self.assertEqual(spec.arms[0].cost_quality_tradeoff, 6)
        self.assertEqual(spec.arms[0].fixed_control_arm_id, "fixed-openai")
        self.assertTrue(spec.arms[0].fallback_enabled)
        self.assertTrue(spec.arms[1].baseline)
        plans, _secrets = compile_route_plans(
            spec,
            environ={"OPENROUTER_API_KEY": "secret"},
            admitted_auth_envs={"OPENROUTER_API_KEY"},
        )
        self.assertEqual(plans[0].track, "model_router")
        self.assertEqual(plans[0].model_match, "exact_revision")

    def test_model_router_rejects_invalid_pool_and_options(self):
        cases = (
            (model_router_manifest(auto_models='["openai/other"]'), "exactly match"),
            (model_router_manifest(auto_providers='["anthropic"]'), "allowed_providers"),
            (model_router_manifest(auto_providers="[]"), "non-empty array"),
            (model_router_manifest(auto_tradeoff="11"), "at most 10"),
            (model_router_manifest(auto_fallback="false"), "fallback_enabled=true"),
            (model_router_manifest(auto_control=""), "fixed_control_arm_id"),
            (model_router_manifest(fixed_baseline="false"), "baseline fixed"),
            (model_router_manifest(fixed_gateway='gateway = "vercel"'), "openrouter"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RouterSpecError, message):
                    parse_experiment_toml(text)

    def test_model_router_rejects_responses_and_mixed_protocols(self):
        responses = model_router_manifest().replace(
            "/chat/completions", "/responses"
        ).replace(
            'protocol = "openai_chat"', 'protocol = "openai_responses"'
        )
        with self.assertRaisesRegex(
            RouterSpecError,
            "model_router supports only protocol='openai_chat'",
        ):
            parse_experiment_toml(responses)

        mixed = responses.replace(
            "https://openrouter.ai/api/v1/responses",
            "https://openrouter.ai/api/v1/chat/completions",
            1,
        ).replace(
            'protocol = "openai_responses"',
            'protocol = "openai_chat"',
            1,
        )
        with self.assertRaisesRegex(
            RouterSpecError,
            "model_router supports only protocol='openai_chat'",
        ):
            parse_experiment_toml(mixed)

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
        with self.assertRaises(RouterSpecError):
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
        self.assertEqual(secrets.value_for("direct-openai"), "direct-secret-value")
        self.assertNotIn("direct-secret-value", repr(secrets))
        with self.assertRaises(TypeError):
            canonical_json(secrets)

    def test_secret_use_requires_explicit_admission_and_present_value(self):
        spec = parse_experiment_toml(manifest())
        with self.assertRaisesRegex(RouterSpecError, "not explicitly admitted"):
            compile_route_plans(
                spec,
                environ={"OPENAI_API_KEY": "x", "OPENROUTER_API_KEY": "y"},
                admitted_auth_envs={"OPENAI_API_KEY"},
            )
        with self.assertRaisesRegex(RouterSpecError, "missing or empty"):
            compile_route_plans(
                spec,
                environ={"OPENAI_API_KEY": "x"},
                admitted_auth_envs={"OPENAI_API_KEY", "OPENROUTER_API_KEY"},
            )

    def test_auth_accepts_environment_variable_names_never_values(self):
        with self.assertRaisesRegex(RouterSpecError, "auth_env"):
            parse_experiment_toml(
                manifest(gateway_auth='auth_env = "sk-live-secret"')
            )
        with self.assertRaisesRegex(RouterSpecError, "unknown field"):
            parse_experiment_toml(
                manifest(gateway_extra='direct_control_arm_id = "direct-openai"\nauth_value = "secret"')
            )

    def test_rejects_unknown_keys_at_every_level(self):
        cases = (
            manifest().replace(
                "schema_version = 1", "schema_version = 1\nunexpected = true", 1),
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
                with self.assertRaisesRegex(RouterSpecError, "unknown field"):
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
                with self.assertRaisesRegex(RouterSpecError, message):
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

    def test_gateway_tax_requires_direct_control_and_equal_fixed_conditions(self):
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
                with self.assertRaisesRegex(RouterSpecError, message):
                    parse_experiment_toml(text)

    def test_rejects_non_https_and_private_endpoint_without_private_admission(self):
        with self.assertRaisesRegex(RouterSpecError, "HTTPS"):
            parse_experiment_toml(manifest().replace("https://api.openai.com", "http://api.openai.com"))
        with self.assertRaisesRegex(RouterSpecError, "private_router"):
            parse_experiment_toml(manifest().replace("api.openai.com", "127.0.0.1"))

    def test_gateway_profiles_are_explicit_and_concentrate_is_admission_blocked(self):
        with self.assertRaisesRegex(RouterSpecError, "requires gateway"):
            parse_experiment_toml(manifest(gateway_extra=(
                'direct_control_arm_id = "direct-openai"'
            )))
        with self.assertRaisesRegex(RouterSpecError, "must not declare gateway"):
            parse_experiment_toml(manifest(direct_extra='gateway = "openrouter"'))
        with self.assertRaisesRegex(
            RouterSpecError,
            "Gateway Tax is unsupported for concentrate",
        ):
            parse_experiment_toml(manifest(gateway_extra=(
                'gateway = "concentrate"\n'
                'direct_control_arm_id = "direct-openai"'
            )))

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
            RouterSpecError,
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
            RouterSpecError,
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
            "private_router = false",
            'private_router = true\nprivate_cidr_allowlist = ["10.0.0.0/8"]',
        ).replace("api.openai.com", "127.0.0.1")
        with self.assertRaisesRegex(RouterSpecError, "not covered"):
            parse_experiment_toml(private)

        admitted = private.replace("10.0.0.0/8", "127.0.0.0/8")
        self.assertEqual(
            parse_experiment_toml(admitted).arms[0].endpoint,
            "https://127.0.0.1/v1/chat/completions",
        )

    def test_private_host_allowlist_is_validated_and_normalized(self):
        valid = manifest().replace(
            "private_router = false",
            'private_router = true\nprivate_host_allowlist = ["Router.Internal."]',
        )
        self.assertEqual(
            parse_experiment_toml(valid).private_host_allowlist,
            ("router.internal",),
        )
        with self.assertRaisesRegex(RouterSpecError, "bare DNS hostname"):
            parse_experiment_toml(valid.replace("Router.Internal.", "bad host:443"))

    def test_window_timestamps_require_rfc3339_not_broader_iso8601(self):
        invalid = manifest().replace(
            "2026-07-22T08:00:00Z", "2026-07-22 08:00:00+00:00"
        )
        with self.assertRaisesRegex(RouterSpecError, "RFC3339"):
            parse_experiment_toml(invalid)

    def test_compile_revalidates_manually_constructed_experiment(self):
        spec = parse_experiment_toml(manifest())
        invalid_arm = dataclasses.replace(spec.arms[0], endpoint="http://127.0.0.1")
        invalid_spec = dataclasses.replace(spec, arms=(invalid_arm, spec.arms[1]))

        with self.assertRaisesRegex(RouterSpecError, "HTTPS"):
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
