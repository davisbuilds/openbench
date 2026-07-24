#!/usr/bin/env python3
"""Contract tests for protocol-aware, privacy-safe gateway metrics."""

import json
import unittest

from obench import gateway_metrics


def sse(*objects, newline="\n"):
    events = []
    for obj in objects:
        data = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
        events.append(f"data: {data}{newline}{newline}")
    return "".join(events).encode()


class GatewayMetricsTests(unittest.TestCase):
    def setUp(self):
        self.secret = "RAW_GENERATED_CONTENT_MUST_NOT_SURVIVE"
        self.role = {
            "model": "openai/gpt-4o-mini",
            "provider": "OpenAI",
            "choices": [{"delta": {"role": "assistant", "content": ""}}],
        }
        self.token = {
            "model": "openai/gpt-4o-mini",
            "provider": "OpenAI",
            "choices": [{"delta": {"content": self.secret}}],
        }
        self.final = {
            "model": "openai/gpt-4o-mini",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            "openrouter_metadata": {
                "requested": "openai/gpt-4o-mini",
                "attempt": 2,
                "endpoints": {"available": [
                    {"provider": "Other", "model": "openai/gpt-4o-mini", "selected": False},
                    {"provider": "OpenAI", "model": "openai/gpt-4o-mini", "selected": True},
                ]},
                "attempts": [
                    {"provider": "OpenAI", "model": "openai/gpt-4o-mini", "status": 200,
                     "private_detail": self.secret},
                ],
                "summary": self.secret,
            },
        }

    def payload(self, newline="\n"):
        prefix = f": keepalive{newline}{newline}event: ping{newline}{newline}"
        empty = {"choices": [{"delta": {"content": ""}}]}
        return prefix.encode() + sse(self.role, empty, self.token, self.final, "[DONE]", newline=newline)

    def parse(self, chunks, completed_at=16.0):
        return gateway_metrics.parse_chat_sse(
            chunks,
            requested_model="openai/gpt-4o-mini",
            started_at=10.0,
            completed_at=completed_at,
            route_kind="gateway",
            requested_provider="OpenAI",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("OpenAI",),
        )

    def test_fragmented_and_coalesced_frames_have_identical_evidence(self):
        payload = self.payload()
        expected = self.parse([(12.0, payload)])
        for split in range(len(payload) + 1):
            with self.subTest(split=split):
                actual = self.parse([(12.0, payload[:split]), (12.0, payload[split:])])
                self.assertEqual(actual, expected)
        bytewise = self.parse([(12.0, payload[index:index + 1]) for index in range(len(payload))])
        self.assertEqual(bytewise, expected)

    def test_crlf_delimiters_can_split_between_cr_and_lf(self):
        payload = self.payload(newline="\r\n")
        parser = gateway_metrics.OpenAIChatSSEParser(
            requested_model="openai/gpt-4o-mini", started_at=10.0
        )
        for index, byte in enumerate(payload):
            parser.feed(bytes([byte]), 11.0 + index / 1000)
        result = parser.finalize(14.0)
        self.assertTrue(result["stream"]["done"])
        self.assertEqual(result["usage"]["output_tokens"], 4)
        self.assertTrue(result["route_evidence"]["pass"])

    def test_ttfb_and_semantic_ttft_use_observation_timestamps(self):
        parser = gateway_metrics.ChatSSEMetricsParser(
            requested_model="openai/gpt-4o-mini", started_at=100.0
        )
        parser.feed(b": comment\n\n", 100.25)
        parser.feed(sse(self.role), 100.5)
        parser.feed(sse(self.token), 101.75)
        parser.feed(sse(self.final, "[DONE]"), 103.0)
        result = parser.finalize(104.0)
        self.assertEqual(result["timing"], {
            "ttfb_s": 0.25,
            "semantic_ttft_s": 1.75,
            "total_s": 4.0,
        })
        self.assertEqual(result["generation"], {
            "output_tokens": 4,
            "duration_s": 2.25,
            "tokens_per_second": 4 / 2.25,
        })

    def test_ttft_uses_data_timestamp_not_later_event_delimiter(self):
        event = sse(self.token)
        delimiter = event[-1:]
        parser = gateway_metrics.OpenAIChatSSEParser(
            requested_model="openai/gpt-4o-mini", started_at=10.0
        )
        parser.feed(event[:-1], 11.25)
        parser.feed(delimiter, 14.0)
        parser.feed(sse(self.final, "[DONE]"), 15.0)
        result = parser.finalize(16.0)
        self.assertEqual(result["timing"]["semantic_ttft_s"], 1.25)
        self.assertEqual(result["generation"]["duration_s"], 4.75)

    def test_zero_timestamp_is_not_replaced_by_delimiter_timestamp(self):
        event = sse(self.token)
        parser = gateway_metrics.OpenAIChatSSEParser(
            requested_model="openai/gpt-4o-mini", started_at=-1.0
        )
        parser.feed(event[:-1], 0.0)
        parser.feed(event[-1:], 2.0)
        result = parser.finalize(3.0)
        self.assertEqual(result["timing"]["semantic_ttft_s"], 1.0)


    def test_comments_role_only_and_empty_events_do_not_count_as_ttft(self):
        result = self.parse([(11.0, (
            b": comment\n\n"
            + sse(self.role)
            + b"data:\n\n"
            + sse({"choices": [{"delta": {"content": []}}]}, "[DONE]")
        ))])
        self.assertIsNone(result["timing"]["semantic_ttft_s"])
        self.assertFalse(result["coverage"]["semantic_ttft"])
        self.assertGreaterEqual(result["stream"]["ignored_events"], 2)

    def test_tool_call_function_delta_establishes_semantic_ttft(self):
        tool_call = {
            "choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"name": "lookup", "arguments": ""},
            }]}}],
        }
        result = self.parse([(11.5, sse(self.role, tool_call, "[DONE]"))])
        self.assertEqual(result["timing"]["semantic_ttft_s"], 1.5)
        self.assertTrue(result["coverage"]["semantic_ttft"])

    def test_usage_is_paired_with_generation_duration(self):
        result = self.parse([
            (10.2, sse(self.token)),
            (12.2, sse(self.final, "[DONE]")),
        ], completed_at=13.2)
        self.assertEqual(result["usage"], {
            "input_tokens": 11,
            "output_tokens": 4,
            "total_tokens": 15,
        })
        self.assertEqual(result["generation"]["output_tokens"], 4)
        self.assertEqual(result["generation"]["duration_s"], 3.0)
        self.assertEqual(result["generation"]["tokens_per_second"], 4 / 3)

    def test_route_evidence_extracts_provider_attempts_and_passes(self):
        result = self.parse([(11.0, self.payload())])
        self.assertEqual(result["route"], {
            "requested_model": "openai/gpt-4o-mini",
            "metadata_requested_model": "openai/gpt-4o-mini",
            "served_model": "openai/gpt-4o-mini",
            "provider": "OpenAI",
            "attempts": [
                {"provider": "OpenAI", "model": "openai/gpt-4o-mini", "status": 200},
            ],
        })
        self.assertEqual(result["route_evidence"], {"pass": True, "verdict": "pass", "reasons": []})
        self.assertEqual(result["coverage"]["covered"], result["coverage"]["total"])

    def test_route_evidence_fails_closed_when_metadata_is_missing(self):
        payload = sse(self.token, {
            "model": "openai/gpt-4o-mini",
            "choices": [{"delta": {}}],
            "usage": {"completion_tokens": 1},
        }, "[DONE]")
        result = self.parse([(11.0, payload)])
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertEqual(result["route_evidence"]["verdict"], "fail")
        self.assertIn("missing_openrouter_metadata", result["route_evidence"]["reasons"])
        self.assertIn("missing_metadata_requested_model", result["route_evidence"]["reasons"])

    def test_gateway_missing_optional_attempts_passes(self):
        final = dict(self.final)
        final["openrouter_metadata"] = dict(self.final["openrouter_metadata"])
        final["openrouter_metadata"].pop("attempts")
        result = self.parse([(11.0, sse(self.token, final, "[DONE]"))])
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(result["route"]["attempts"], [])

    def test_gateway_wrong_selected_provider_fails(self):
        final = dict(self.final)
        final["openrouter_metadata"] = dict(self.final["openrouter_metadata"])
        final["openrouter_metadata"]["endpoints"] = {
            "available": [{
                "provider": "Different",
                "model": "openai/gpt-4o-mini",
                "selected": True,
            }],
        }
        final["openrouter_metadata"].pop("attempts")
        token = dict(self.token)
        token.pop("provider")
        result = self.parse([(11.0, sse(token, final, "[DONE]"))])
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("provider_conflict", result["route_evidence"]["reasons"])
        self.assertIn("provider_not_allowed", result["route_evidence"]["reasons"])

    def test_gateway_fallback_attempt_fails(self):
        final = dict(self.final)
        final["openrouter_metadata"] = dict(self.final["openrouter_metadata"])
        final["openrouter_metadata"]["attempts"] = [
            {"provider": "Different", "model": "openai/gpt-4o-mini", "status": 503},
            {"provider": "OpenAI", "model": "openai/gpt-4o-mini", "status": 200},
        ]
        result = self.parse([(11.0, sse(self.token, final, "[DONE]"))])
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("fallback_attempt", result["route_evidence"]["reasons"])
        self.assertIn("unsuccessful_attempt", result["route_evidence"]["reasons"])

    def test_gateway_same_route_unsuccessful_attempt_fails(self):
        final = dict(self.final)
        final["openrouter_metadata"] = dict(self.final["openrouter_metadata"])
        final["openrouter_metadata"]["attempts"] = [
            {"provider": "OpenAI", "model": "openai/gpt-4o-mini", "status": 503},
            {"provider": "OpenAI", "model": "openai/gpt-4o-mini", "status": 200},
        ]
        result = self.parse([(11.0, sse(self.token, final, "[DONE]"))])
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("unsuccessful_attempt", result["route_evidence"]["reasons"])

    def test_gateway_explicit_empty_attempts_is_optional(self):
        final = dict(self.final)
        final["openrouter_metadata"] = dict(self.final["openrouter_metadata"])
        final["openrouter_metadata"]["attempts"] = []
        result = self.parse([(11.0, sse(self.token, final, "[DONE]"))])
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertFalse(result["coverage"]["attempt_evidence"])

    def test_gateway_malformed_attempts_fail(self):
        final = dict(self.final)
        final["openrouter_metadata"] = dict(self.final["openrouter_metadata"])
        final["openrouter_metadata"]["attempts"] = ["malformed"]
        result = self.parse([(11.0, sse(self.token, final, "[DONE]"))])
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("malformed_attempts", result["route_evidence"]["reasons"])

    def test_direct_exact_served_model_and_done_pass_without_gateway_evidence(self):
        result = gateway_metrics.parse_chat_sse(
            [(11.0, sse(self.token, "[DONE]"))],
            requested_model="openai/gpt-4o-mini",
            started_at=10.0,
            completed_at=12.0,
            route_kind="direct",
            requested_provider="OpenAI",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("OpenAI",),
        )
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertFalse(result["coverage"]["openrouter_metadata"])
        self.assertFalse(result["coverage"]["attempts"])

    def test_direct_rolling_alias_accepts_resolved_dated_snapshot(self):
        token = dict(self.token)
        token["model"] = "gpt-4o-mini-2024-07-18"
        result = gateway_metrics.parse_chat_sse(
            [(11.0, sse(token, "[DONE]"))],
            requested_model="gpt-4o-mini",
            started_at=10.0,
            completed_at=12.0,
            route_kind="direct",
            requested_provider="OpenAI",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("OpenAI",),
            model_match="rolling_alias",
        )
        self.assertTrue(result["route_evidence"]["pass"])

    def test_direct_rolling_alias_rejects_conflicting_provider_qualification(self):
        token = dict(self.token)
        token["model"] = "anthropic/gpt-4o-mini"
        result = gateway_metrics.parse_chat_sse(
            [(11.0, sse(token, "[DONE]"))],
            requested_model="gpt-4o-mini",
            started_at=10.0,
            completed_at=12.0,
            route_kind="direct",
            requested_provider="OpenAI",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("OpenAI",),
            model_match="rolling_alias",
        )
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("served_model_conflict", result["route_evidence"]["reasons"])

    def test_direct_rolling_alias_rejects_two_snapshots_across_stream(self):
        first = dict(self.token)
        first["model"] = "gpt-4o-mini-2024-07-18"
        second = dict(self.token)
        second["model"] = "gpt-4o-mini-2024-08-01"
        result = gateway_metrics.parse_chat_sse(
            [(11.0, sse(first, second, "[DONE]"))],
            requested_model="gpt-4o-mini",
            started_at=10.0,
            completed_at=12.0,
            route_kind="direct",
            requested_provider="OpenAI",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("OpenAI",),
            model_match="rolling_alias",
        )
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("served_model_conflict", result["route_evidence"]["reasons"])

    def test_direct_rejects_completed_stream_without_served_model(self):
        result = gateway_metrics.parse_chat_sse(
            [(11.0, sse({"choices": [{"delta": {"content": "x"}}]}, "[DONE]"))],
            requested_model="gpt-4o-mini",
            started_at=10.0,
            completed_at=12.0,
            route_kind="direct",
            requested_provider="OpenAI",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("OpenAI",),
            model_match="rolling_alias",
        )
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("missing_served_model", result["route_evidence"]["reasons"])

    def test_malformed_event_does_not_poison_later_events(self):
        payload = b"data: {not-json}\n\n" + self.payload()
        result = self.parse([(11.0, payload)])
        self.assertEqual(result["stream"]["malformed_events"], 1)
        self.assertEqual(result["usage"]["output_tokens"], 4)
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("malformed_events", result["route_evidence"]["reasons"])

    def test_route_evidence_requires_done_marker(self):
        result = self.parse([(11.0, sse(self.token, self.final))])
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("stream_not_done", result["route_evidence"]["reasons"])

    def test_no_raw_prompt_or_output_content_is_retained(self):
        parser = gateway_metrics.OpenAIChatSSEParser(
            requested_model="openai/gpt-4o-mini", started_at=10.0
        )
        parser.feed(self.payload(), 11.0)
        result = parser.finalize(12.0)
        self.assertNotIn(self.secret, json.dumps(result, sort_keys=True))
        self.assertNotIn(self.secret, repr(vars(parser)))
        self.assertNotIn("summary", json.dumps(result, sort_keys=True))
        self.assertNotIn("private_detail", json.dumps(result, sort_keys=True))

    def test_finalize_purges_truncated_raw_event_content(self):
        parser = gateway_metrics.OpenAIChatSSEParser(
            requested_model="openai/gpt-4o-mini", started_at=10.0
        )
        parser.feed(("data: " + json.dumps(self.token)).encode(), 11.0)
        result = parser.finalize(12.0)
        self.assertEqual(result["stream"]["malformed_events"], 1)
        self.assertNotIn(self.secret, repr(vars(parser)))
        self.assertFalse(result["route_evidence"]["pass"])

    def test_rejects_non_monotonic_timestamps_and_feed_after_finalize(self):
        parser = gateway_metrics.OpenAIChatSSEParser(requested_model="model", started_at=5.0)
        parser.feed(b"data: ", 6.0)
        parser.feed(b"", 6.5)
        with self.assertRaises(ValueError):
            parser.feed(b"{}\n\n", 6.25)
        parser.finalize(7.0)
        with self.assertRaises(RuntimeError):
            parser.feed(b"", 7.0)

    def test_rejects_mixed_bytes_and_text_chunks(self):
        parser = gateway_metrics.OpenAIChatSSEParser(requested_model="model", started_at=5.0)
        parser.feed(b"data: ", 6.0)
        with self.assertRaises(TypeError):
            parser.feed("{}\n\n", 6.5)


class ResponsesGatewayMetricsTests(unittest.TestCase):
    def test_nested_response_model_usage_and_terminal_event_pass(self):
        secret = "RESPONSES_OUTPUT_MUST_NOT_SURVIVE"
        payload = sse(
            {
                "type": "response.created",
                "response": {"model": "gpt-4o-mini-2024-07-18"},
            },
            {"type": "response.output_text.delta", "delta": secret},
            {
                "type": "response.completed",
                "response": {
                    "model": "gpt-4o-mini-2024-07-18",
                    "usage": {
                        "input_tokens": 13,
                        "input_tokens_details": {
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                        },
                        "output_tokens": 3,
                        "total_tokens": 16,
                    },
                },
            },
        )
        result = gateway_metrics.parse_responses_sse(
            [(10.5, payload)],
            requested_model="gpt-4o-mini",
            started_at=10.0,
            completed_at=11.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )
        self.assertEqual(result["route"]["served_model"], "gpt-4o-mini-2024-07-18")
        self.assertEqual(result["usage"], {
            "input_tokens": 13,
            "input_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 0,
            },
            "output_tokens": 3,
            "total_tokens": 16,
        })
        self.assertEqual(result["timing"]["semantic_ttft_s"], 0.5)
        self.assertTrue(result["stream"]["done"])
        self.assertEqual(result["stream"]["terminal_status"], "completed")
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertNotIn(secret, json.dumps(result))

    def test_incomplete_responses_stream_fails_closed(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse({
                "type": "response.output_text.delta",
                "delta": "partial",
            }))],
            requested_model="gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )
        self.assertFalse(result["route_evidence"]["pass"])
        self.assertIn("stream_not_done", result["route_evidence"]["reasons"])
        self.assertIn("missing_served_model", result["route_evidence"]["reasons"])

    def test_response_incomplete_is_a_terminal_event(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse({
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "model": "gpt-4o-mini-2024-07-18",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {
                        "input_tokens": 13,
                        "output_tokens": 16_384,
                        "total_tokens": 16_397,
                    },
                },
            }))],
            requested_model="gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertTrue(result["stream"]["done"])
        self.assertEqual(result["stream"]["terminal_status"], "incomplete")
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(result["usage"]["output_tokens"], 16_384)

    def test_response_failed_is_a_terminal_event(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse({
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "model": "gpt-4o-mini-2024-07-18",
                    "error": {
                        "code": "server_error",
                        "message": "generation failed",
                    },
                },
            }))],
            requested_model="gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertTrue(result["stream"]["done"])
        self.assertEqual(result["stream"]["terminal_status"], "failed")
        self.assertTrue(result["route_evidence"]["pass"])

    def test_response_cancelled_is_a_terminal_event(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse({
                "type": "response.cancelled",
                "response": {
                    "status": "cancelled",
                    "model": "gpt-4o-mini-2024-07-18",
                },
            }))],
            requested_model="gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )

        self.assertTrue(result["stream"]["done"])
        self.assertEqual(result["stream"]["terminal_status"], "cancelled")
        self.assertTrue(result["route_evidence"]["pass"])

    def test_done_sentinel_does_not_replace_response_completed(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse(
                {
                    "type": "response.created",
                    "response": {"model": "gpt-4o-mini-2024-07-18"},
                },
                "[DONE]",
            ))],
            requested_model="gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
        )
        self.assertFalse(result["stream"]["done"])
        self.assertIn("stream_not_done", result["route_evidence"]["reasons"])

    def test_cloudflare_responses_derives_locked_provider(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse(
                {
                    "type": "response.created",
                    "response": {"model": "gpt-4o-mini-2024-07-18"},
                },
                {"type": "response.output_text.delta", "delta": "ok"},
                {
                    "type": "response.completed",
                    "response": {
                        "model": "gpt-4o-mini-2024-07-18",
                        "usage": {"input_tokens": 5, "output_tokens": 1},
                    },
                },
            ))],
            requested_model="openai/gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="gateway",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
            gateway="cloudflare",
        )
        self.assertEqual(result["route"]["provider"], "openai")
        self.assertEqual(result["route"]["served_model"], "gpt-4o-mini-2024-07-18")
        self.assertTrue(result["route_evidence"]["pass"])

    def test_concentrate_responses_derives_provider_without_fabricating_metadata(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse(
                {
                    "type": "response.created",
                    "response": {"model": "openai/gpt-4o-mini"},
                },
                {"type": "response.output_text.delta", "delta": "ok"},
                {
                    "type": "response.completed",
                    "response": {
                        "model": "openai/gpt-4o-mini",
                        "usage": {
                            "input_tokens": 5,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 1,
                        },
                    },
                },
            ))],
            requested_model="openai/gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="gateway",
            requested_provider="openai",
            allowed_models=("openai/gpt-4o-mini",),
            allowed_providers=("openai",),
            model_match="rolling_alias",
            gateway="concentrate",
        )
        self.assertEqual(result["route"]["provider"], "openai")
        self.assertEqual(result["route"]["served_model"], "openai/gpt-4o-mini")
        self.assertIsNone(result["route"]["metadata_requested_model"])
        self.assertEqual(result["route"]["attempts"], [])
        self.assertFalse(result["coverage"]["attempt_evidence"])
        self.assertTrue(result["route_evidence"]["pass"])

    def test_responses_cache_activity_is_preserved_without_route_invalidation(self):
        result = gateway_metrics.parse_responses_sse(
            [(1.0, sse({
                "type": "response.completed",
                "response": {
                    "model": "gpt-4o-mini",
                    "usage": {
                        "input_tokens": 12,
                        "input_tokens_details": {
                            "cached_tokens": 8,
                            "cache_write_tokens": 3,
                        },
                        "output_tokens": 2,
                        "total_tokens": 14,
                    },
                },
            }))],
            requested_model="gpt-4o-mini",
            started_at=0.0,
            completed_at=2.0,
            route_kind="direct",
            requested_provider="openai",
            allowed_models=("gpt-4o-mini",),
            allowed_providers=("openai",),
        )
        self.assertEqual(result["usage"]["input_tokens_details"], {
            "cached_tokens": 8,
            "cache_write_tokens": 3,
        })
        self.assertTrue(result["route_evidence"]["pass"])
        self.assertEqual(result["route_evidence"]["reasons"], [])


if __name__ == "__main__":
    unittest.main()
