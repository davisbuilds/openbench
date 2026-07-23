import json
import unittest

from obench import router_gateways, router_metrics


def sse(*objects):
    return "".join(
        f"data: {json.dumps(obj, separators=(',', ':'))}\n\n"
        if obj != "[DONE]"
        else "data: [DONE]\n\n"
        for obj in objects
    ).encode()


class GatewayRequestProfileTests(unittest.TestCase):
    def base_body(self):
        return {
            "model": "client/model",
            "messages": [{
                "role": "user",
                "content": "private prompt",
                "cache_control": {"type": "ephemeral"},
            }],
            "provider": {"only": ["attacker"], "allow_fallbacks": True},
            "models": ["fallback/model"],
            "order": ["attacker"],
            "sort": "price",
            "caching": "auto",
            "cache": True,
            "prompt_cache_key": "attacker-key",
        }

    def test_openrouter_request_contract_is_unchanged(self):
        body = self.base_body()
        router_gateways.shape_body(
            body, gateway="openrouter", requested_provider="openai"
        )
        self.assertEqual(body["provider"], {
            "only": ["openai"],
            "allow_fallbacks": False,
        })
        self.assertNotIn("cache", body)
        self.assertNotIn("prompt_cache_key", body)
        self.assertNotIn("cache_control", body["messages"][0])
        self.assertEqual(
            router_gateways.request_headers(
                gateway="openrouter", gateway_id=None, secret="secret"
            ),
            {
                "Authorization": "Bearer secret",
                "X-OpenRouter-Metadata": "enabled",
                "X-OpenRouter-Cache": "false",
            },
        )

    def test_vercel_sends_only_provider_filter_and_no_routing_or_cache_options(self):
        body = self.base_body()
        body["providerOptions"] = {
            "gateway": {"models": ["fallback"], "order": ["other"], "caching": "auto"},
            "openai": {"reasoningEffort": "low"},
        }
        router_gateways.shape_body(
            body, gateway="vercel", requested_provider="openai"
        )
        self.assertEqual(body["providerOptions"], {
            "gateway": {"only": ["openai"]},
        })
        for key in ("provider", "models", "order", "sort", "caching", "cache"):
            self.assertNotIn(key, body)
        self.assertNotIn("cache_control", body["messages"][0])
        self.assertEqual(
            router_gateways.request_headers(
                gateway="vercel", gateway_id=None, secret="secret"
            ),
            {"Authorization": "Bearer secret"},
        )

    def test_cloudflare_forces_gateway_retry_cache_and_payload_log_headers(self):
        body = self.base_body()
        router_gateways.shape_body(
            body, gateway="cloudflare", requested_provider="openai"
        )
        for key in (
            "provider", "providerOptions", "models", "order", "sort",
            "caching", "cache", "prompt_cache_key",
        ):
            self.assertNotIn(key, body)
        self.assertEqual(
            router_gateways.request_headers(
                gateway="cloudflare",
                gateway_id="strict-tax",
                secret="secret",
            ),
            {
                "Authorization": "Bearer secret",
                "cf-aig-gateway-id": "strict-tax",
                "cf-aig-skip-cache": "true",
                "cf-aig-max-attempts": "1",
                "cf-aig-collect-log": "true",
                "cf-aig-collect-log-payload": "false",
            },
        )


class GatewayEvidenceTests(unittest.TestCase):
    def parse(self, payload, **kwargs):
        return router_metrics.parse_chat_sse(
            [(11.0, payload)],
            requested_model=kwargs.pop("requested_model"),
            requested_provider=kwargs.pop("requested_provider"),
            allowed_models=kwargs.pop("allowed_models"),
            allowed_providers=kwargs.pop("allowed_providers"),
            gateway=kwargs.pop("gateway"),
            response_headers=kwargs.pop("response_headers", {}),
            started_at=10.0,
            completed_at=12.0,
            **kwargs,
        )

    def test_vercel_documented_route_and_single_attempt_pass(self):
        result = self.parse(
            sse(
                {"model": "openai/gpt-4o-mini", "choices": [{"delta": {"content": "x"}}]},
                {
                    "providerMetadata": {
                        "gateway": {
                            "finalProvider": "openai",
                            "resolvedProviderApiModelId": "gpt-4o-mini-2024-07-18",
                            "modelAttemptCount": 1,
                            "totalProviderAttemptCount": 1,
                            "modelAttempts": [{
                                "providerAttempts": [{
                                    "provider": "openai",
                                    "resolvedProviderApiModelId": "gpt-4o-mini-2024-07-18",
                                    "statusCode": 200,
                                }],
                            }],
                            "generationId": "gen_public",
                            "cost": 0.001,
                            "marketCost": 0.002,
                        },
                    },
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
                "[DONE]",
            ),
            gateway="vercel",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini", "gpt-4o-mini-2024-07-18"),
            allowed_providers=("openai",),
        )
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(result["route"]["provider"], "openai")
        self.assertEqual(result["route"]["served_model"], "gpt-4o-mini-2024-07-18")
        self.assertEqual(len(result["route"]["attempts"]), 1)
        self.assertEqual(result["route"]["gateway_metadata"], {
            "generationId": "gen_public",
            "cost": 0.001,
            "marketCost": 0.002,
        })

    def test_vercel_fails_closed_on_missing_or_contradictory_attempt_evidence(self):
        base = {
            "finalProvider": "other",
            "resolvedProviderApiModelId": "gpt-4o-mini-2024-07-18",
            "modelAttemptCount": 2,
            "modelAttempts": [],
        }
        result = self.parse(
            sse(
                {"providerMetadata": {"gateway": base}},
                "[DONE]",
            ),
            gateway="vercel",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini-2024-07-18",),
            allowed_providers=("openai",),
        )
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("provider_conflict", result["route_evidence"]["reasons"])
        self.assertIn("multiple_attempts", result["route_evidence"]["reasons"])
        self.assertIn("missing_model_attempts", result["route_evidence"]["reasons"])

    def test_cloudflare_headers_prove_route_without_persisting_unrelated_secrets(self):
        result = self.parse(
            sse(
                {"model": "openai/gpt-4o-mini", "choices": [{"delta": {"content": "x"}}]},
                {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
                "[DONE]",
            ),
            gateway="cloudflare",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini-2024-07-18",),
            allowed_providers=("openai",),
            response_headers={
                "cf-aig-provider": "openai",
                "cf-aig-model": "openai/gpt-4o-mini-2024-07-18",
                "cf-aig-step": "0",
                "cf-aig-cache-status": "BYPASS",
                "cf-aig-log-id": "log_public",
                "set-cookie": "private-secret",
            },
        )
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(
            result["route"]["served_model"],
            "openai/gpt-4o-mini-2024-07-18",
        )
        self.assertEqual(result["route"]["gateway_metadata"], {
            "log_id": "log_public",
            "cache_status": "BYPASS",
            "step": 0,
        })
        self.assertNotIn("private-secret", json.dumps(result, sort_keys=True))

    def test_cloudflare_fails_closed_without_provider_or_for_fallback_step(self):
        result = self.parse(
            sse({"choices": [{"delta": {"content": "x"}}]}, "[DONE]"),
            gateway="cloudflare",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
            response_headers={
                "cf-aig-model": "openai/gpt-4o-mini",
                "cf-aig-step": "1",
            },
        )
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("missing_provider", result["route_evidence"]["reasons"])
        self.assertIn("fallback_attempt", result["route_evidence"]["reasons"])


if __name__ == "__main__":
    unittest.main()
