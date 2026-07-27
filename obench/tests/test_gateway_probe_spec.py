import dataclasses
import unittest

from obench import gateway_probe_spec


def manifest(prompt="Return the word probe."):
    return f"""
schema_version = 1
experiment_id = "probe-test"
track = "request_probe"
model_match = "exact_revision"
repetitions = 2
schedule_seed = 17
allow_private_endpoint = false

[budget]
timeout_s = 30
max_output_tokens = 64
usd_cap = 0.05

[[cases]]
case_id = "short"
prompt = {prompt!r}

[[arms]]
arm_id = "direct"
route_kind = "direct"
endpoint = "https://api.openai.com/v1/responses"
protocol = "openai_responses"
baseline = true
canonical_model = "openai/gpt-4o-mini"
requested_model = "gpt-4o-mini"
requested_provider = "openai"
allowed_models = ["gpt-4o-mini"]
allowed_providers = ["openai"]
fallback_enabled = false
retry_count = 0
cache_enabled = false
auth_env = "OPENAI_API_KEY"
[arms.sampling]
temperature = 0.0
top_p = 1.0
seed = 17

[[arms]]
arm_id = "gateway"
route_kind = "gateway"
gateway = "openrouter"
direct_control_arm_id = "direct"
endpoint = "https://openrouter.ai/api/v1/responses"
protocol = "openai_responses"
baseline = false
canonical_model = "openai/gpt-4o-mini"
requested_model = "openai/gpt-4o-mini"
requested_provider = "openai"
allowed_models = ["openai/gpt-4o-mini"]
allowed_providers = ["openai"]
fallback_enabled = false
retry_count = 0
cache_enabled = false
auth_env = "OPENROUTER_API_KEY"
[arms.sampling]
temperature = 0.0
top_p = 1.0
seed = 17
"""


class GatewayProbeSpecTests(unittest.TestCase):
    def test_parses_and_digest_binds_fixed_prompt(self):
        first = gateway_probe_spec.parse_experiment_toml(manifest())
        second = gateway_probe_spec.parse_experiment_toml(manifest("Different prompt."))
        self.assertEqual(first.track, "request_probe")
        self.assertNotEqual(first.digest, second.digest)
        self.assertNotEqual(first.cases[0].prompt_digest, second.cases[0].prompt_digest)
        self.assertEqual({arm.protocol for arm in first.arms}, {"openai_responses"})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.repetitions = 3

    def test_rejects_agent_fields_and_route_policy_drift(self):
        with self.assertRaisesRegex(
            gateway_probe_spec.GatewayProbeSpecError, "unknown field: harness"
        ):
            gateway_probe_spec.parse_experiment_toml(
                manifest().replace('track = "request_probe"', 'track = "request_probe"\nharness = "pi"')
            )
        with self.assertRaisesRegex(
            gateway_probe_spec.GatewayProbeSpecError, "retry_count"
        ):
            gateway_probe_spec.parse_experiment_toml(
                manifest().replace("retry_count = 0", "retry_count = 1", 1)
            )

    def test_compile_reuses_secret_isolation_and_rebinds_probe_track(self):
        experiment = gateway_probe_spec.parse_experiment_toml(manifest())
        plans, secrets = gateway_probe_spec.compile_route_plans(
            experiment,
            environ={"OPENAI_API_KEY": "direct-secret", "OPENROUTER_API_KEY": "gateway-secret"},
            admitted_auth_envs={"OPENAI_API_KEY", "OPENROUTER_API_KEY"},
        )
        self.assertEqual({plan.track for plan in plans}, {"request_probe"})
        self.assertTrue(all(plan.experiment_digest == experiment.digest for plan in plans))
        persisted = gateway_probe_spec.gateway_spec.canonical_json(
            [plan.to_dict() for plan in plans]
        )
        self.assertNotIn("direct-secret", persisted)
        self.assertNotIn("gateway-secret", persisted)
        self.assertEqual(secrets.value_for("direct"), "direct-secret")

    def test_compile_revalidates_manually_constructed_probe(self):
        experiment = gateway_probe_spec.parse_experiment_toml(manifest())
        invalid_arm = dataclasses.replace(experiment.arms[0], retry_count=1)
        invalid = dataclasses.replace(
            experiment, arms=(invalid_arm, experiment.arms[1])
        )
        with self.assertRaisesRegex(
            gateway_probe_spec.GatewayProbeSpecError, "retry_count"
        ):
            gateway_probe_spec.compile_route_plans(
                invalid,
                environ={
                    "OPENAI_API_KEY": "direct-secret",
                    "OPENROUTER_API_KEY": "gateway-secret",
                },
                admitted_auth_envs={
                    "OPENAI_API_KEY", "OPENROUTER_API_KEY"
                },
            )


if __name__ == "__main__":
    unittest.main()
