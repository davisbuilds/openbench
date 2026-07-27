import json
import unittest

from obench import gateway_profiles, gateway_metrics


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

    def test_openrouter_fixed_profile_replaces_routing_and_session_controls(self):
        body = self.base_body()
        body.update({
            "plugins": [{"id": "auto-router"}],
            "router": {"strategy": "attacker"},
            "session_id": "attacker-session",
            "conversation_id": "attacker-conversation",
            "providerOptions": {"gateway": {"only": ["attacker"]}},
        })
        gateway_profiles.shape_body(
            body, gateway="openrouter", requested_provider="openai"
        )
        self.assertEqual(body["provider"], {
            "only": ["openai"],
            "allow_fallbacks": False,
        })
        self.assertNotIn("cache", body)
        self.assertNotIn("prompt_cache_key", body)
        self.assertNotIn("cache_control", body["messages"][0])
        for key in (
            "plugins", "router", "session_id", "conversation_id",
            "providerOptions", "models", "order", "sort", "caching",
        ):
            self.assertNotIn(key, body)
        self.assertEqual(
            gateway_profiles.request_headers(
                gateway="openrouter", secret="secret"
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
        gateway_profiles.shape_body(
            body, gateway="vercel", requested_provider="openai"
        )
        self.assertEqual(body["providerOptions"], {
            "gateway": {"only": ["openai"]},
        })
        for key in ("provider", "models", "order", "sort", "caching", "cache"):
            self.assertNotIn(key, body)
        self.assertNotIn("cache_control", body["messages"][0])
        self.assertEqual(
            gateway_profiles.request_headers(
                gateway="vercel", secret="secret"
            ),
            {"Authorization": "Bearer secret"},
        )

    def test_cloudflare_profile_requires_real_account_and_qualified_model(self):
        rest_endpoint = (
            "https://api.cloudflare.com/client/v4/accounts/"
            "0123456789abcdef0123456789abcdef/ai/v1/chat/completions"
        )
        compat_endpoint = (
            "https://gateway.ai.cloudflare.com/v1/"
            "0123456789abcdef0123456789abcdef/"
            "openbench-gateway-bench/compat/chat/completions"
        )
        gateway_profiles.validate_arm(
            route_kind="gateway",
            gateway="cloudflare",
            gateway_id="openbench-gateway-bench",
            endpoint=rest_endpoint,
            protocol="openai_chat",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
        )

        invalid_endpoints = (
            rest_endpoint.replace(
                "0123456789abcdef0123456789abcdef", "{account_id}"
            ),
            rest_endpoint.replace(
                "0123456789abcdef0123456789abcdef", "account-id"
            ),
            rest_endpoint + "?gateway=other",
            compat_endpoint.replace("openbench-gateway-bench", "{gateway_id}"),
            compat_endpoint,
        )
        for invalid in invalid_endpoints:
            with self.subTest(endpoint=invalid):
                with self.assertRaisesRegex(
                    gateway_profiles.GatewayProfileError,
                    "cloudflare managed endpoint must be",
                ):
                    gateway_profiles.validate_arm(
                        route_kind="gateway",
                        gateway="cloudflare",
                        gateway_id="openbench-gateway-bench",
                        endpoint=invalid,
                        protocol="openai_chat",
                        requested_model="openai/gpt-4o-mini",
                        requested_provider="openai",
                    )

        with self.assertRaisesRegex(
            gateway_profiles.GatewayProfileError,
            "provider-qualified with requested_provider",
        ):
            gateway_profiles.validate_arm(
                route_kind="gateway",
                gateway="cloudflare",
                gateway_id="openbench-gateway-bench",
                endpoint=rest_endpoint,
                protocol="openai_chat",
                requested_model="anthropic/gpt-4o-mini",
                requested_provider="openai",
            )
        responses_endpoint = rest_endpoint.replace(
            "/chat/completions", "/responses"
        )
        gateway_profiles.validate_arm(
            route_kind="gateway",
            gateway="cloudflare",
            gateway_id="openbench-gateway-bench",
            endpoint=responses_endpoint,
            protocol="openai_responses",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
        )
        with self.assertRaisesRegex(
            gateway_profiles.GatewayProfileError,
            "cloudflare managed endpoint must be",
        ):
            gateway_profiles.validate_arm(
                route_kind="gateway",
                gateway="cloudflare",
                gateway_id="openbench-gateway-bench",
                endpoint=responses_endpoint,
                protocol="openai_chat",
                requested_model="openai/gpt-4o-mini",
                requested_provider="openai",
            )

        with self.assertRaisesRegex(
            gateway_profiles.GatewayProfileError,
            "requires a valid gateway_id",
        ):
            gateway_profiles.validate_arm(
                route_kind="gateway",
                gateway="cloudflare",
                endpoint=responses_endpoint,
                protocol="openai_responses",
                requested_model="openai/gpt-4o-mini",
                requested_provider="openai",
            )
        with self.assertRaisesRegex(
            gateway_profiles.GatewayProfileError,
            "cloudflare managed endpoint must be",
        ):
            gateway_profiles.validate_arm(
                route_kind="gateway",
                gateway="cloudflare",
                gateway_id="openbench-gateway-bench",
                endpoint=compat_endpoint,
                protocol="openai_chat",
                requested_model="openai/gpt-4o-mini",
                requested_provider="openai",
                allow_private_endpoint=True,
            )
    def test_cloudflare_overwrites_headers_and_strips_body_controls(self):
        body = self.base_body()
        body.update({
            "providerOptions": {"gateway": {"only": ["attacker"]}},
            "router": {"strategy": "attacker"},
            "plugins": [{"id": "attacker"}],
            "routes": [{"provider": "attacker"}],
            "fallback": {"model": "attacker/model"},
        })

        gateway_profiles.shape_body(
            body, gateway="cloudflare", requested_provider="openai"
        )

        for key in (
            "provider", "providerOptions", "models", "order", "sort", "caching",
            "router", "plugins", "routes", "fallback", "cache",
            "prompt_cache_key",
        ):
            self.assertNotIn(key, body)
        self.assertNotIn("cache_control", body["messages"][0])
        self.assertEqual(
            gateway_profiles.request_headers(
                gateway="cloudflare",
                gateway_id="openbench-gateway-bench",
                secret="secret",
            ),
            {
                "Authorization": "Bearer secret",
                "cf-aig-gateway-id": "openbench-gateway-bench",
                "cf-aig-skip-cache": "true",
                "cf-aig-max-attempts": "1",
                "cf-aig-collect-log-payload": "false",
            },
        )
        for name in (
            "cf-aig-gateway-id",
            "CF-AIG-CACHE-TTL",
            "cf-aig-cache-key",
            "cf-aig-max-attempts",
            "cf-aig-retry-delay",
            "cf-aig-backoff",
            "cf-aig-collect-log",
            "cf-aig-collect-log-payload",
            "cf-aig-metadata",
        ):
            with self.subTest(header=name):
                self.assertTrue(gateway_profiles.blocked_request_header(name))

    def test_concentrate_requires_exact_responses_route_and_qualified_model(self):
        valid = {
            "route_kind": "gateway",
            "gateway": "concentrate",
            "endpoint": "https://api.concentrate.ai/v1/responses",
            "protocol": "openai_responses",
            "requested_model": "openai/gpt-4o-mini",
            "requested_provider": "openai",
        }
        gateway_profiles.validate_arm(**valid)

        invalid = (
            ({"endpoint": valid["endpoint"] + "/"}, "supports only"),
            ({"protocol": "openai_chat"}, "supports only"),
            ({"requested_model": "gpt-4o-mini"}, "provider-qualified"),
            ({"requested_model": "azure/gpt-4o-mini"}, "provider-qualified"),
        )
        for changes, message in invalid:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(
                    gateway_profiles.GatewayProfileError, message
                ):
                    gateway_profiles.validate_arm(**{**valid, **changes})

    def test_concentrate_replaces_hostile_routing_cache_and_fallback_controls(self):
        body = self.base_body()
        body.update({
            "routing": {
                "providers": ["attacker"],
                "models": ["fallback/model"],
            },
            "providerOptions": {"gateway": {"only": ["attacker"]}},
            "router": {"strategy": "attacker"},
            "plugins": [{"id": "attacker"}],
            "routes": [{"provider": "attacker"}],
            "fallback": {"model": "fallback/model"},
            "fallbacks": ["fallback/model"],
            "prompt_cache_options": {"mode": "explicit"},
            "prompt_cache_retention": "24h",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "configure",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "caching": {"type": "boolean"},
                            "cache_control": {"type": "string"},
                        },
                        "required": ["caching", "cache_control"],
                    },
                },
            }],
        })

        gateway_profiles.shape_body(
            body, gateway="concentrate", requested_provider="openai"
        )

        self.assertEqual(body["routing"], {
            "providers": ["openai"],
            "models": [],
        })
        for key in (
            "provider", "providerOptions", "models", "order", "sort", "caching",
            "router", "plugins", "routes", "fallback", "fallbacks", "cache",
            "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention",
        ):
            self.assertNotIn(key, body)
        self.assertNotIn("cache_control", body["messages"][0])
        self.assertEqual(
            body["tools"][0]["function"]["parameters"]["required"],
            ["caching", "cache_control"],
        )
        self.assertIn(
            "caching",
            body["tools"][0]["function"]["parameters"]["properties"],
        )
        self.assertIn(
            "cache_control",
            body["tools"][0]["function"]["parameters"]["properties"],
        )
        self.assertEqual(
            gateway_profiles.request_headers(
                gateway="concentrate", secret="secret"
            ),
            {"Authorization": "Bearer secret"},
        )

    def test_model_match_distinguishes_exact_revision_from_rolling_alias(self):
        requested = "openai/gpt-5.6-2026-07-01"
        observed = "openai/gpt-5.6"
        self.assertFalse(
            gateway_profiles.models_match(requested, observed, "exact_revision")
        )
        self.assertTrue(
            gateway_profiles.models_match(requested, observed, "rolling_alias")
        )
        self.assertTrue(
            gateway_profiles.models_match(requested, observed, "model_family")
        )
        self.assertFalse(
            gateway_profiles.models_match(
                "openai/gpt-4o-mini", "anthropic/claude-haiku-4.5", "rolling_alias"
            )
        )
        self.assertFalse(
            gateway_profiles.models_match(
                "openai/gpt-4o-mini",
                "anthropic/gpt-4o-mini",
                "rolling_alias",
            )
        )
        self.assertTrue(
            gateway_profiles.models_match(
                "openai/gpt-4o-mini",
                "anthropic/gpt-4o-mini",
                "model_family",
            )
        )
        self.assertFalse(
            gateway_profiles.models_match(
                "gpt-4o-mini-2024-07-18",
                "gpt-4o-mini-2024-08-01",
                "rolling_alias",
            )
        )
        self.assertTrue(
            gateway_profiles.model_evidence_consistent(
                "openai/gpt-4o-mini",
                "gpt-4o-mini-2024-07-18",
                "rolling_alias",
            )
        )
        self.assertFalse(
            gateway_profiles.model_evidence_consistent(
                "gpt-4o-mini-2024-07-18",
                "gpt-4o-mini-2024-08-01",
                "rolling_alias",
            )
        )
        self.assertFalse(
            gateway_profiles.model_evidence_consistent(
                "openai/gpt-4o-mini",
                "anthropic/gpt-4o-mini-2024-07-18",
                "rolling_alias",
            )
        )

class GatewayEvidenceTests(unittest.TestCase):
    def parse(self, payload, **kwargs):
        return gateway_metrics.parse_chat_sse(
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
            model_match="model_family",
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

    def test_vercel_family_match_accepts_canonical_alias_without_exact_claim(self):
        requested = "openai/gpt-5.6-2026-07-01"
        alias = "openai/gpt-5.6"
        payload = sse(
            {
                "model": requested,
                "choices": [{
                    "delta": {
                        "content": "x",
                        "provider_metadata": {
                            "gateway": {
                                "routing": {
                                    "originalModelId": requested,
                                    "finalProvider": "openai",
                                    "canonicalSlug": alias,
                                    "modelAttemptCount": 1,
                                    "totalProviderAttemptCount": 1,
                                    "modelAttempts": [{
                                        "canonicalSlug": alias,
                                        "success": True,
                                        "providerAttempts": [{
                                            "provider": "openai",
                                            "success": True,
                                            "statusCode": 200,
                                        }],
                                    }],
                                },
                            },
                        },
                    },
                }],
            },
            "[DONE]",
        )
        common = {
            "gateway": "vercel",
            "requested_model": requested,
            "requested_provider": "openai",
            "allowed_models": (requested,),
            "allowed_providers": ("openai",),
        }

        family = self.parse(payload, model_match="model_family", **common)
        exact = self.parse(payload, model_match="exact_revision", **common)

        self.assertTrue(family["route_evidence"]["pass"])
        self.assertEqual(family["route"]["served_model"], alias)
        self.assertFalse(exact["route_evidence"]["pass"])
        self.assertIn(
            "served_model_not_allowed", exact["route_evidence"]["reasons"]
        )

    def test_vercel_rolling_alias_accepts_snapshot_and_alias_evidence_fields(self):
        alias = "openai/gpt-4o-mini"
        snapshot = "gpt-4o-mini-2024-07-18"
        payload = sse(
            {"model": snapshot, "choices": [{"delta": {"content": "x"}}]},
            {
                "providerMetadata": {
                    "gateway": {
                        "finalProvider": "openai",
                        "canonicalSlug": alias,
                        "resolvedProviderApiModelId": snapshot,
                        "modelAttemptCount": 1,
                        "totalProviderAttemptCount": 1,
                        "modelAttempts": [{
                            "canonicalSlug": alias,
                            "success": True,
                            "providerAttempts": [{
                                "provider": "openai",
                                "resolvedProviderApiModelId": snapshot,
                                "statusCode": 200,
                            }],
                        }],
                    },
                },
            },
            "[DONE]",
        )
        common = {
            "gateway": "vercel",
            "requested_model": alias,
            "requested_provider": "openai",
            "allowed_models": (alias,),
            "allowed_providers": ("openai",),
        }

        rolling = self.parse(payload, model_match="rolling_alias", **common)
        exact = self.parse(payload, model_match="exact_revision", **common)

        self.assertTrue(rolling["route_evidence"]["pass"])
        self.assertFalse(exact["route_evidence"]["pass"])

    def test_vercel_rolling_alias_rejects_two_snapshots_bridged_by_alias(self):
        alias = "openai/gpt-4o-mini"
        payload = sse(
            {
                "model": "gpt-4o-mini-2024-07-18",
                "choices": [{"delta": {"content": "x"}}],
            },
            {
                "providerMetadata": {
                    "gateway": {
                        "finalProvider": "openai",
                        "canonicalSlug": alias,
                        "modelAttemptCount": 1,
                        "totalProviderAttemptCount": 1,
                        "modelAttempts": [{
                            "canonicalSlug": alias,
                            "success": True,
                            "providerAttempts": [{
                                "provider": "openai",
                                "resolvedProviderApiModelId": "gpt-4o-mini-2024-08-01",
                                "statusCode": 200,
                            }],
                        }],
                    },
                },
            },
            "[DONE]",
        )
        result = self.parse(
            payload,
            gateway="vercel",
            requested_model=alias,
            requested_provider="openai",
            allowed_models=(alias,),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("served_model_conflict", result["route_evidence"]["reasons"])

    def test_vercel_records_provider_model_even_with_canonical_alias(self):
        alias = "openai/gpt-4o-mini"
        result = self.parse(
            sse(
                {
                    "model": alias,
                    "choices": [{"delta": {"content": "x"}}],
                    "providerMetadata": {
                        "gateway": {
                            "finalProvider": "openai",
                            "canonicalSlug": alias,
                            "resolvedProviderApiModelId": "gpt-4o-mini-2024-07-18",
                            "modelAttemptCount": 1,
                            "totalProviderAttemptCount": 1,
                            "modelAttempts": [{
                                "canonicalSlug": alias,
                                "success": True,
                                "providerAttempts": [{
                                    "provider": "openai",
                                    "resolvedProviderApiModelId": "gpt-4o-mini-2024-08-01",
                                    "statusCode": 200,
                                }],
                            }],
                        },
                    },
                },
                "[DONE]",
            ),
            gateway="vercel",
            requested_model=alias,
            requested_provider="openai",
            allowed_models=(alias,),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("served_model_conflict", result["route_evidence"]["reasons"])

    def test_openrouter_keeps_only_valid_streamed_usage_cost(self):
        private_value = "private-usage-detail"
        result = self.parse(
            sse(
                {
                    "model": "openai/gpt-4o-mini",
                    "provider": "OpenAI",
                    "choices": [{"delta": {"content": "x"}}],
                },
                {
                    "model": "openai/gpt-4o-mini",
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "cost": 0.00125,
                        "private": private_value,
                    },
                    "openrouter_metadata": {
                        "requested": "openai/gpt-4o-mini",
                        "endpoints": {"available": [{
                            "provider": "OpenAI",
                            "selected": True,
                            "private": private_value,
                        }]},
                    },
                },
                "[DONE]",
            ),
            gateway="openrouter",
            requested_model="openai/gpt-4o-mini",
            requested_provider="OpenAI",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("OpenAI",),
        )

        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(result["route"]["gateway_metadata"], {"cost": 0.00125})
        self.assertNotIn(private_value, json.dumps(result, sort_keys=True))

    def test_openrouter_rejects_selected_endpoint_from_other_model_family(self):
        requested = "openai/gpt-4o-mini"
        result = self.parse(
            sse(
                {
                    "model": requested,
                    "provider": "OpenAI",
                    "choices": [{"delta": {"content": "x"}}],
                    "openrouter_metadata": {
                        "requested": requested,
                        "endpoints": {"available": [{
                            "provider": "OpenAI",
                            "model": "anthropic/claude-haiku-4.5",
                            "selected": True,
                        }]},
                        "attempts": [{
                            "provider": "OpenAI",
                            "model": requested,
                            "status": 200,
                        }],
                    },
                },
                "[DONE]",
            ),
            gateway="openrouter",
            requested_model=requested,
            requested_provider="openai",
            allowed_models=(requested,),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("served_model_conflict", result["route_evidence"]["reasons"])

    def test_openrouter_omits_missing_or_invalid_streamed_usage_cost(self):
        cases = {
            "missing": {},
            "malformed": {"cost": "0.00125"},
            "negative": {"cost": -0.00125},
            "bool": {"cost": True},
            "non_finite": {"cost": float("inf")},
        }
        for name, usage_values in cases.items():
            with self.subTest(name=name):
                result = self.parse(
                    sse(
                        {
                            "model": "openai/gpt-4o-mini",
                            "provider": "OpenAI",
                            "choices": [{"delta": {"content": "x"}}],
                        },
                        {
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "cost": 0.99,
                            },
                        },
                        {
                            "model": "openai/gpt-4o-mini",
                            "usage": {
                                "prompt_tokens": 2,
                                "completion_tokens": 1,
                                **usage_values,
                            },
                            "openrouter_metadata": {
                                "requested": "openai/gpt-4o-mini",
                            },
                        },
                        "[DONE]",
                    ),
                    gateway="openrouter",
                    requested_model="openai/gpt-4o-mini",
                    requested_provider="OpenAI",
                    allowed_models=("openai/gpt-4o-mini",),
                    allowed_providers=("OpenAI",),
                )
                self.assertNotIn("gateway_metadata", result["route"])

    def test_vercel_live_delta_routing_shape_passes_without_private_metadata(self):
        result = self.parse(
            sse(
                {
                    "model": "openai/gpt-4o-mini",
                    "choices": [{
                        "delta": {
                            "content": "x",
                            "provider_metadata": {
                                "gateway": {
                                    "routing": {
                                        "originalModelId": "openai/gpt-4o-mini",
                                        "resolvedProvider": "openai",
                                        "finalProvider": "openai",
                                        "canonicalSlug": "openai/gpt-4o-mini",
                                        "modelAttemptCount": 1,
                                        "totalProviderAttemptCount": 1,
                                        "planningReasoning": "private-plan",
                                        "modelAttempts": [{
                                            "canonicalSlug": "openai/gpt-4o-mini",
                                            "success": True,
                                            "providerAttempts": [{
                                                "provider": "openai",
                                                "credentialType": "private-credential",
                                                "success": True,
                                                "statusCode": 200,
                                                "providerRequestId": "private-request-id",
                                                "providerResponseId": "private-response-id",
                                            }],
                                        }],
                                    },
                                    "generationId": "gen_live",
                                    "cost": 0.001,
                                    "marketCost": 0.002,
                                },
                            },
                        },
                    }],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
                "[DONE]",
            ),
            gateway="vercel",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
        )

        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(
            result["route"],
            {
                "requested_model": "openai/gpt-4o-mini",
                "metadata_requested_model": "openai/gpt-4o-mini",
                "served_model": "openai/gpt-4o-mini",
                "provider": "openai",
                "attempts": [{
                    "provider": "openai",
                    "model": "openai/gpt-4o-mini",
                    "status": 200,
                }],
                "gateway_metadata": {
                    "generationId": "gen_live",
                    "cost": 0.001,
                    "marketCost": 0.002,
                },
            },
        )
        serialized = json.dumps(result, sort_keys=True)
        for private_value in (
            "private-plan",
            "private-credential",
            "private-request-id",
            "private-response-id",
        ):
            self.assertNotIn(private_value, serialized)

    def test_vercel_live_delta_rejects_wrong_request_and_multiple_attempts(self):
        result = self.parse(
            sse(
                {
                    "model": "openai/gpt-4o-mini",
                    "choices": [{
                        "delta": {
                            "provider_metadata": {
                                "gateway": {
                                    "routing": {
                                        "originalModelId": "openai/other-model",
                                        "resolvedProvider": "anthropic",
                                        "finalProvider": "openai",
                                        "canonicalSlug": "openai/gpt-4o-mini-revision",
                                        "modelAttemptCount": 1,
                                        "totalProviderAttemptCount": 2,
                                        "modelAttempts": [{
                                            "canonicalSlug": "openai/gpt-4o-mini-revision",
                                            "success": True,
                                            "providerAttempts": [
                                                {
                                                    "provider": "openai",
                                                    "success": True,
                                                    "statusCode": 200,
                                                },
                                                {
                                                    "provider": "anthropic",
                                                    "success": True,
                                                    "statusCode": 200,
                                                },
                                            ],
                                        }],
                                    },
                                },
                            },
                        },
                    }],
                },
                "[DONE]",
            ),
            gateway="vercel",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
        )

        self.assertFalse(result["route_evidence"]["pass"])
        reasons = result["route_evidence"]["reasons"]
        self.assertIn("requested_model_conflict", reasons)
        self.assertIn("provider_conflict", reasons)
        self.assertIn("multiple_attempts", reasons)
        self.assertIn("fallback_attempt", reasons)
        self.assertIn("served_model_not_allowed", reasons)

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

    def test_cloudflare_derives_provider_from_request_and_keeps_served_model(self):
        result = self.parse(
            sse(
                {
                    "model": "gpt-4o-mini-2024-07-18",
                    "choices": [{"delta": {"content": "x"}}],
                },
                "[DONE]",
            ),
            gateway="cloudflare",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(result["route"]["provider"], "openai")
        self.assertEqual(
            result["route"]["served_model"],
            "gpt-4o-mini-2024-07-18",
        )
        self.assertNotIn("gateway_metadata", result["route"])

    def test_cloudflare_rejects_missing_or_wrong_served_model_and_provider(self):
        cases = (
            ({}, "openai/gpt-4o-mini", "missing_served_model"),
            (
                {"model": "anthropic/gpt-4o-mini"},
                "openai/gpt-4o-mini",
                "provider_conflict",
            ),
            (
                {"model": "openai/gpt-4.1-mini"},
                "openai/gpt-4o-mini",
                "served_model_not_allowed",
            ),
            (
                {"model": "gpt-4o-mini-2024-08-01"},
                "openai/gpt-4o-mini-2024-07-18",
                "served_model_not_allowed",
            ),
        )
        for event, requested_model, expected_reason in cases:
            with self.subTest(event=event):
                result = self.parse(
                    sse(event, "[DONE]"),
                    gateway="cloudflare",
                    requested_model=requested_model,
                    requested_provider="openai",
                    allowed_models=("openai/gpt-4o-mini",),
                    allowed_providers=("openai",),
                    model_match="rolling_alias",
                )
                self.assertFalse(result["route_evidence"]["pass"])
                self.assertIn(
                    expected_reason, result["route_evidence"]["reasons"]
                )

    def test_concentrate_requires_provider_qualified_final_model(self):
        valid = self.parse(
            sse({"model": "openai/gpt-4o-mini"}, "[DONE]"),
            gateway="concentrate",
            requested_model="openai/gpt-4o-mini",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
        )
        self.assertTrue(valid["route_evidence"]["pass"])
        self.assertEqual(valid["route"]["provider"], "openai")
        self.assertIsNone(valid["route"]["metadata_requested_model"])
        self.assertEqual(valid["route"]["attempts"], [])
        self.assertFalse(valid["coverage"]["attempt_evidence"])

        cases = (
            ("gpt-4o-mini", "unqualified_served_model"),
            ("azure/gpt-4o-mini", "provider_conflict"),
            ("openai/gpt-4.1-mini", "served_model_not_allowed"),
        )
        for observed, expected_reason in cases:
            with self.subTest(observed=observed):
                result = self.parse(
                    sse({"model": observed}, "[DONE]"),
                    gateway="concentrate",
                    requested_model="openai/gpt-4o-mini",
                    requested_provider="openai",
                    allowed_models=("openai/gpt-4o-mini",),
                    allowed_providers=("openai",),
                )
                self.assertFalse(result["route_evidence"]["pass"])
                self.assertIn(
                    expected_reason, result["route_evidence"]["reasons"]
                )

if __name__ == "__main__":
    unittest.main()
