import dataclasses
import http.client
import json
import socket
import threading
import time
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from obench import (
    gateway_profiles,
    gateway_probe_http,
    gateway_probe_spec,
    gateway_run,
    gateway_spec,
)
from obench.gateway_probe_models import ProbeBlock


PRIVATE_PROMPT = "PRIVATE_PROBE_PROMPT_MUST_NOT_PERSIST"
PRIVATE_OUTPUT = "PRIVATE_MODEL_OUTPUT_MUST_NOT_PERSIST"


class _SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []
    fail_first = False
    close_measured = False
    chat_finish_reasons = []

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        size = int(self.headers["content-length"])
        payload = json.loads(self.rfile.read(size))
        type(self).requests.append({
            "connection": id(self.connection),
            "payload": payload,
            "headers": {
                key.lower(): value for key, value in self.headers.items()
            },
        })
        if self.path == "/chat/completions":
            request_index = len(type(self).requests) - 1
            finish_reason = (
                self.chat_finish_reasons[request_index]
                if request_index < len(self.chat_finish_reasons)
                else "stop"
            )
            events = (
                {
                    "model": "gpt-test",
                    "provider": "openai",
                    "choices": [{"delta": {"content": PRIVATE_OUTPUT}}],
                },
                {
                    "model": "gpt-test",
                    "provider": "openai",
                    "choices": [{
                        "delta": {},
                        "finish_reason": finish_reason,
                    }],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 3,
                        "total_tokens": 8,
                    },
                },
            )
            body = (
                "".join(
                    f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    for event in events
                )
                + "data: [DONE]\n\n"
            ).encode()
        else:
            terminal = (
                "failed"
                if self.fail_first and len(self.requests) == 1
                else "completed"
            )
            body = (
                'data: {"type":"response.output_text.delta","delta":"'
                + PRIVATE_OUTPUT
                + '"}\n\n'
                f'data: {{"type":"response.{terminal}","response":'
                f'{{"status":"{terminal}","model":"gpt-test","provider":"openai",'
                '"usage":{"input_tokens":5,"output_tokens":3,"total_tokens":8}}}\n\n'
            ).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-request-id", "receipt-123")
        self.send_header("x-internal-debug", "must-not-persist")
        close_response = self.close_measured and len(self.requests) == 2
        self.send_header(
            "connection", "close" if close_response else "keep-alive"
        )
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


def experiment(endpoint, *, protocol="openai_responses"):
    arm = gateway_spec.Arm(
        arm_id="direct",
        route_kind="direct",
        endpoint=endpoint,
        protocol=protocol,
        baseline=True,
        canonical_model="openai/gpt-test",
        requested_model="gpt-test",
        requested_provider="openai",
        allowed_models=("gpt-test",),
        allowed_providers=("openai",),
        fallback_enabled=False,
        retry_count=0,
        cache_enabled=False,
        auth_env="TEST_KEY",
        sampling=gateway_spec.Sampling(0.0, 1.0, 7),
    )
    return gateway_probe_spec.GatewayProbeExperiment(
        schema_version=1,
        experiment_id="local",
        track="request_probe",
        model_match="exact_revision",
        repetitions=1,
        schedule_seed=9,
        allow_private_endpoint=True,
        private_host_allowlist=(),
        private_cidr_allowlist=("127.0.0.1/32",),
        budget=gateway_probe_spec.ProbeBudget(5, 16, "1"),
        cases=(gateway_probe_spec.ProbeCase("case", PRIVATE_PROMPT),),
        arms=(arm,),
    )


def route_plan(exp, endpoint):
    return gateway_spec.RoutePlan(
        schema_version=1,
        experiment_digest=exp.digest,
        arm_digest=exp.arms[0].digest,
        arm_id="direct",
        route_kind="direct",
        endpoint=endpoint,
        protocol=exp.arms[0].protocol,
        canonical_model="openai/gpt-test",
        requested_model="gpt-test",
        requested_provider="openai",
        allowed_models=("gpt-test",),
        allowed_providers=("openai",),
        fallback_enabled=False,
        retry_count=0,
        cache_enabled=False,
        auth_env="TEST_KEY",
        sampling=exp.arms[0].sampling,
        allow_private_endpoint=True,
        private_host_allowlist=(),
        private_cidr_allowlist=("127.0.0.1/32",),
        track="request_probe",
    )


def prices():
    return {
        "openai/gpt-test": gateway_run.Price(
            Decimal("1"),
            Decimal("2"),
            "2026-07-25T00:00:00Z",
        )
    }


class GatewayProbeHttpTests(unittest.TestCase):
    def setUp(self):
        _SSEHandler.requests = []
        _SSEHandler.fail_first = False
        _SSEHandler.close_measured = False
        _SSEHandler.chat_finish_reasons = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.endpoint = (
            f"http://127.0.0.1:{self.server.server_port}/responses"
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_warm_primer_and_measurement_use_same_socket_without_leaking_payloads(self):
        exp = experiment(self.endpoint)
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "warm", 1, ("direct",)
        )
        result = gateway_probe_http.execute_request(
            experiment=exp,
            case=exp.cases[0],
            block=block,
            plan=route_plan(exp, self.endpoint),
            secret="test-secret",
            prices=prices(),
        )
        self.assertTrue(result["reuse_evidence"]["socket_reused"])
        self.assertTrue(result["outcome"]["success"])
        self.assertEqual(result["route_integrity"]["status"], "verified")
        self.assertEqual(len(_SSEHandler.requests), 2)
        self.assertFalse(_SSEHandler.requests[0]["payload"]["store"])
        self.assertFalse(_SSEHandler.requests[1]["payload"]["store"])
        self.assertEqual(
            _SSEHandler.requests[0]["connection"],
            _SSEHandler.requests[1]["connection"],
        )
        primer_input = _SSEHandler.requests[0]["payload"]["input"]
        measured_input = _SSEHandler.requests[1]["payload"]["input"]
        self.assertNotEqual(primer_input, measured_input)
        self.assertTrue(primer_input.startswith("[openbench_probe_nonce:"))
        self.assertNotEqual(
            primer_input.split("\n", 1)[0],
            measured_input.split("\n", 1)[0],
        )
        self.assertIn(PRIVATE_PROMPT, primer_input)
        self.assertIn(PRIVATE_PROMPT, measured_input)
        serialized = json.dumps(result, sort_keys=True)
        for private in (PRIVATE_PROMPT, PRIVATE_OUTPUT, "test-secret"):
            self.assertNotIn(private, serialized)
        self.assertNotEqual(
            result["reuse_evidence"]["primer_nonce_sha256"],
            result["reuse_evidence"]["measured_nonce_sha256"],
        )
        self.assertIsNotNone(
            result["reuse_evidence"]["setup"]["dns_s"]
        )
        self.assertIsNone(result["request_metrics"]["setup"])
        timing = result["request_metrics"]["timing"]
        self.assertIsNotNone(timing["request_to_response_headers_s"])
        self.assertIsNotNone(timing["request_to_first_body_byte_s"])
        self.assertIsNotNone(timing["request_to_semantic_ttft_s"])
        self.assertIsNotNone(timing["request_stream_total_s"])
        self.assertTrue(
            all(
                timing[name] is None
                for name in (
                    "cold_end_to_end_response_headers_s",
                    "cold_end_to_end_first_body_byte_s",
                    "cold_end_to_end_semantic_ttft_s",
                    "cold_end_to_end_stream_total_s",
                )
            )
        )
        self.assertEqual(
            result["request_metrics"]["receipt_headers"],
            {"x-request-id": "receipt-123"},
        )
        self.assertEqual(
            result["reuse_evidence"]["receipt_headers"],
            {"x-request-id": "receipt-123"},
        )
        self.assertGreater(
            Decimal(result["billing"]["primer_cost_usd"]), 0
        )
        self.assertGreater(
            Decimal(result["billing"]["charged_cost_usd"]),
            Decimal(result["billing"]["measured_cost_usd"]),
        )

    def test_warm_primer_retains_bounded_completion_stream_evidence(self):
        _SSEHandler.chat_finish_reasons = ["stop", "length"]
        endpoint = self.endpoint.replace("/responses", "/chat/completions")
        exp = experiment(endpoint, protocol="openai_chat")
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "warm", 1, ("direct",)
        )

        result = gateway_probe_http.execute_request(
            experiment=exp,
            case=exp.cases[0],
            block=block,
            plan=route_plan(exp, endpoint),
            secret="test-secret",
            prices=prices(),
        )

        self.assertTrue(result["outcome"]["success"])
        self.assertEqual(
            result["reuse_evidence"]["stream"],
            {
                "done": True,
                "terminal_status": "completed",
                "finish_reason": "stop",
                "finalized": True,
            },
        )
        self.assertEqual(
            result["request_metrics"]["stream"]["finish_reason"],
            "length",
        )
        self.assertNotIn(PRIVATE_OUTPUT, json.dumps(result, sort_keys=True))

    def test_cloudflare_managed_probe_sends_bound_gateway_controls(self):
        exp = experiment(self.endpoint)
        plan = dataclasses.replace(
            route_plan(exp, self.endpoint),
            arm_id="cloudflare-managed",
            route_kind="gateway",
            requested_model="openai/gpt-test",
            allowed_models=("openai/gpt-test",),
            gateway="cloudflare",
            gateway_id="openbench-gateway-bench",
        )
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "cold", 1, (plan.arm_id,)
        )

        result = gateway_probe_http.execute_request(
            experiment=exp,
            case=exp.cases[0],
            block=block,
            plan=plan,
            secret="cloudflare-managed-secret",
            prices=prices(),
        )

        self.assertTrue(result["outcome"]["success"])
        headers = _SSEHandler.requests[-1]["headers"]
        self.assertEqual(
            headers["authorization"],
            "Bearer cloudflare-managed-secret",
        )
        self.assertEqual(
            headers["cf-aig-gateway-id"],
            "openbench-gateway-bench",
        )
        self.assertEqual(headers["cf-aig-skip-cache"], "true")
        self.assertEqual(headers["cf-aig-max-attempts"], "1")
        self.assertNotIn("cloudflare-managed-secret", json.dumps(result))

    def test_warm_measurement_requires_a_successful_verified_primer(self):
        _SSEHandler.fail_first = True
        exp = experiment(self.endpoint)
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "warm", 1, ("direct",)
        )
        result = gateway_probe_http.execute_request(
            experiment=exp,
            case=exp.cases[0],
            block=block,
            plan=route_plan(exp, self.endpoint),
            secret="test-secret",
            prices=prices(),
        )
        self.assertEqual(len(_SSEHandler.requests), 1)
        self.assertFalse(result["outcome"]["attempted"])
        self.assertEqual(result["outcome"]["error_class"], "primer")
        self.assertFalse(result["reuse_evidence"]["completed"])
        self.assertGreater(
            Decimal(result["billing"]["primer_cost_usd"]), 0
        )

    def test_cold_phase_boundaries_include_setup_exactly_once(self):
        exp = experiment(self.endpoint)
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "cold", 1, ("direct",)
        )
        result = gateway_probe_http.execute_request(
            experiment=exp,
            case=exp.cases[0],
            block=block,
            plan=route_plan(exp, self.endpoint),
            secret="test-secret",
            prices=prices(),
        )
        setup = result["request_metrics"]["setup"]
        timing = result["request_metrics"]["timing"]
        self.assertIsNotNone(setup["dns_s"])
        self.assertIsNotNone(setup["tcp_s"])
        self.assertIsNone(setup["tls_s"])
        pairs = (
            (
                "cold_end_to_end_response_headers_s",
                "request_to_response_headers_s",
            ),
            (
                "cold_end_to_end_first_body_byte_s",
                "request_to_first_body_byte_s",
            ),
            (
                "cold_end_to_end_semantic_ttft_s",
                "request_to_semantic_ttft_s",
            ),
            (
                "cold_end_to_end_stream_total_s",
                "request_stream_total_s",
            ),
        )
        for end_to_end, request_only in pairs:
            self.assertGreaterEqual(timing[end_to_end], timing[request_only])
        self.assertLessEqual(
            timing["request_to_response_headers_s"],
            timing["request_to_first_body_byte_s"],
        )
        self.assertLessEqual(
            timing["request_to_first_body_byte_s"],
            timing["request_to_semantic_ttft_s"],
        )

    def test_consume_uses_post_send_and_pre_dns_clock_boundaries(self):
        class FakeSocket:
            def settimeout(self, _value):
                return None

        class FakeResponse:
            status = 200
            will_close = False

            def __init__(self):
                self.chunks = [b"data", b""]

            def getheaders(self):
                return [("content-type", "text/event-stream")]

            def read1(self, _size):
                return self.chunks.pop(0)

        class FakeConnection:
            timeout = 30
            sock = FakeSocket()

            def set_request_deadline(self, deadline):
                self.deadline = deadline

            def request(self, *_args, **_kwargs):
                return None

            def _remaining_timeout(self):
                return 20

            def getresponse(self):
                return FakeResponse()

        class FakeParser:
            def feed(self, _chunk, received_at):
                self.received_at = received_at

            def finalize(self, completed_at):
                self.completed_at = completed_at
                return {
                    "timing": {
                        "ttfb_s": 6.0,
                        "semantic_ttft_s": 7.0,
                    }
                }

        exp = experiment(self.endpoint)
        parser = FakeParser()
        with (
            mock.patch.object(
                gateway_probe_http.time,
                "monotonic",
                side_effect=[10.0, 12.0, 15.0, 16.0, 18.0, 19.0, 20.0],
            ),
            mock.patch.object(
                gateway_probe_http.gateway_metrics,
                "sse_parser",
                return_value=parser,
            ) as parser_factory,
        ):
            _status, _metrics, _closed, evidence = gateway_probe_http._consume(
                FakeConnection(),
                "/responses",
                b"{}",
                {},
                route_plan(exp, self.endpoint),
                capture_metrics=True,
                cold_started_at=9.0,
            )
        self.assertEqual(parser_factory.call_args.kwargs["started_at"], 12.0)
        self.assertEqual(evidence["timing"], {
            "request_to_response_headers_s": 3.0,
            "request_to_first_body_byte_s": 6.0,
            "request_to_semantic_ttft_s": 7.0,
            "request_stream_total_s": 8.0,
            "cold_end_to_end_response_headers_s": 6.0,
            "cold_end_to_end_first_body_byte_s": 9.0,
            "cold_end_to_end_semantic_ttft_s": 10.0,
            "cold_end_to_end_stream_total_s": 11.0,
        })

    def test_receipt_headers_use_strict_allowlist_and_value_sanitation(self):
        receipts = gateway_probe_http._receipt_headers([
            ("X-Request-ID", " safe-id "),
            ("Authorization", "secret"),
            ("CF-Ray", "bad\nvalue"),
            ("X-Vercel-ID", "x" * 257),
            ("Request-ID", "first"),
            ("Request-ID", "second"),
            ("OpenAI-Request-ID", "openai-id"),
            ("Anthropic-Request-ID", "private output words"),
        ])
        self.assertEqual(receipts, {
            "x-request-id": "safe-id",
            "openai-request-id": "openai-id",
        })

    def test_warm_reuse_survives_measured_response_closing_same_socket(self):
        _SSEHandler.close_measured = True
        exp = experiment(self.endpoint)
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "warm", 1, ("direct",)
        )
        result = gateway_probe_http.execute_request(
            experiment=exp,
            case=exp.cases[0],
            block=block,
            plan=route_plan(exp, self.endpoint),
            secret="test-secret",
            prices=prices(),
        )
        self.assertEqual(len(_SSEHandler.requests), 2)
        self.assertEqual(
            _SSEHandler.requests[0]["connection"],
            _SSEHandler.requests[1]["connection"],
        )
        self.assertTrue(result["reuse_evidence"]["socket_reused"])
        self.assertTrue(result["outcome"]["success"])

    def test_slow_dns_is_bounded_by_one_request_deadline(self):
        exp = experiment(self.endpoint)
        exp = dataclasses.replace(
            exp,
            budget=dataclasses.replace(exp.budget, timeout_s=0.1),
        )
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "cold", 1, ("direct",)
        )
        original_getaddrinfo = socket.getaddrinfo

        def slow_dns(*args, **kwargs):
            time.sleep(0.35)
            return original_getaddrinfo(*args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(
            gateway_probe_http.socket,
            "getaddrinfo",
            side_effect=slow_dns,
        ):
            result = gateway_probe_http.execute_request(
                experiment=exp,
                case=exp.cases[0],
                block=block,
                plan=route_plan(exp, self.endpoint),
                secret="test-secret",
                prices=prices(),
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result["outcome"]["timed_out"], result)
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 0.25)
        time.sleep(0.3)
        self.assertEqual(_SSEHandler.requests, [])

    def test_multiple_address_attempts_share_one_request_deadline(self):
        exp = experiment(self.endpoint)
        exp = dataclasses.replace(
            exp,
            budget=dataclasses.replace(exp.budget, timeout_s=0.2),
        )
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "cold", 1, ("direct",)
        )
        sockets = []

        class SlowFailingSocket:
            def __init__(self, *_args):
                self.index = len(sockets)
                self.timeout = None
                sockets.append(self)

            def settimeout(self, value):
                self.timeout = value

            def connect(self, _sockaddr):
                if self.index == 0:
                    time.sleep(0.08)
                    raise ConnectionRefusedError
                time.sleep(self.timeout)
                raise socket.timeout

            def close(self):
                return None

        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 2)),
        ]
        started = time.monotonic()
        with (
            mock.patch.object(
                gateway_probe_http.socket,
                "getaddrinfo",
                return_value=addresses,
            ),
            mock.patch.object(
                gateway_probe_http.socket,
                "socket",
                SlowFailingSocket,
            ),
        ):
            result = gateway_probe_http.execute_request(
                experiment=exp,
                case=exp.cases[0],
                block=block,
                plan=route_plan(exp, self.endpoint),
                secret="test-secret",
                prices=prices(),
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result["outcome"]["timed_out"], result)
        self.assertEqual(len(sockets), 2)
        self.assertLess(sockets[1].timeout, sockets[0].timeout)
        self.assertGreaterEqual(elapsed, 0.16)
        self.assertLess(elapsed, 0.26)

    def test_gateway_body_shaping_is_authoritative(self):
        plan = gateway_spec.RoutePlan(
            schema_version=1,
            experiment_digest="e" * 64,
            arm_digest="a" * 64,
            arm_id="gateway",
            route_kind="gateway",
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            protocol="openai_chat",
            canonical_model="openai/gpt-test",
            requested_model="openai/gpt-test",
            requested_provider="openai",
            allowed_models=("openai/gpt-test",),
            allowed_providers=("openai",),
            fallback_enabled=False,
            retry_count=0,
            cache_enabled=False,
            auth_env="KEY",
            sampling=gateway_spec.Sampling(0.0, 1.0, 9),
            allow_private_endpoint=False,
            private_host_allowlist=(),
            private_cidr_allowlist=(),
            gateway="openrouter",
            track="request_probe",
        )
        body = json.loads(
            gateway_probe_http.request_body("prompt", "nonce", plan, 32)
        )
        self.assertEqual(body["model"], "openai/gpt-test")
        self.assertEqual(
            body["provider"], {"only": ["openai"], "allow_fallbacks": False}
        )
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_completion_tokens"], 32)
        responses_plan = dataclasses.replace(
            plan,
            endpoint="https://openrouter.ai/api/v1/responses",
            protocol="openai_responses",
        )
        responses_body = json.loads(
            gateway_probe_http.request_body(
                "prompt", "nonce", responses_plan, 32
            )
        )
        self.assertIs(responses_body["store"], False)
        self.assertEqual(
            responses_body["provider"],
            {"only": ["openai"], "allow_fallbacks": False},
        )

    def test_kimi_examples_compile_exact_five_way_chat_route_locks(self):
        examples = Path(__file__).parents[1] / "examples"
        experiment = gateway_probe_spec.load_experiment(
            examples / "gateway-probe-kimi-k3-five-way-chat.toml"
        )
        auth_envs = {arm.auth_env for arm in experiment.arms}
        plans, _secrets = gateway_probe_spec.compile_route_plans(
            experiment,
            environ={name: f"secret-for-{name}" for name in auth_envs},
            admitted_auth_envs=auth_envs,
        )
        by_id = {plan.arm_id: plan for plan in plans}

        self.assertEqual(experiment.repetitions, 5)
        self.assertEqual(experiment.budget.max_output_tokens, 128)
        self.assertEqual(len(plans), 5)
        self.assertEqual({plan.protocol for plan in plans}, {"openai_chat"})
        self.assertEqual(
            {arm_id: plan.endpoint for arm_id, plan in by_id.items()},
            {
                "direct-moonshot":
                    "https://api.moonshot.ai/v1/chat/completions",
                "openrouter-moonshot":
                    "https://openrouter.ai/api/v1/chat/completions",
                "vercel-moonshot":
                    "https://ai-gateway.vercel.sh/v1/chat/completions",
                "concentrate-moonshot":
                    "https://api.concentrate.ai/v1/chat/completions",
                "cloudflare-moonshot":
                    "https://api.cloudflare.com/client/v4/accounts/"
                    "0123456789abcdef0123456789abcdef/ai/v1/chat/completions",
            },
        )
        bodies = {
            arm_id: json.loads(
                gateway_probe_http.request_body("prompt", "nonce", plan, 128)
            )
            for arm_id, plan in by_id.items()
        }
        self.assertEqual(
            bodies["openrouter-moonshot"]["provider"],
            {"only": ["moonshotai"], "allow_fallbacks": False},
        )
        self.assertEqual(
            bodies["vercel-moonshot"]["providerOptions"],
            {"gateway": {"only": ["moonshotai"]}},
        )
        self.assertEqual(
            bodies["concentrate-moonshot"]["routing"],
            {"providers": ["moonshot"], "models": []},
        )
        self.assertEqual(
            by_id["concentrate-moonshot"].requested_model,
            "moonshot/kimi-k3",
        )
        self.assertEqual(
            {
                arm_id: body["model"]
                for arm_id, body in bodies.items()
            },
            {
                "direct-moonshot": "kimi-k3",
                "openrouter-moonshot": "moonshotai/kimi-k3",
                "vercel-moonshot": "moonshotai/kimi-k3",
                "concentrate-moonshot": "moonshot/kimi-k3",
                "cloudflare-moonshot": "moonshotai/kimi-k3",
            },
        )
        cloudflare = by_id["cloudflare-moonshot"]
        self.assertEqual(cloudflare.gateway_id, "openbench-router-bench")
        self.assertNotIn("provider", bodies["cloudflare-moonshot"])
        self.assertEqual(
            gateway_profiles.request_headers(
                gateway=cloudflare.gateway,
                gateway_id=cloudflare.gateway_id,
                secret="secret",
            )["cf-aig-gateway-id"],
            "openbench-router-bench",
        )
        self.assertTrue(
            all(body["max_completion_tokens"] == 128 for body in bodies.values())
        )

        gateway_bench = gateway_spec.load_experiment(
            examples / "gateway-bench-kimi-k3-five-way-chat.toml"
        )
        self.assertEqual(gateway_bench.repetitions_per_window, 5)
        self.assertEqual(len(gateway_bench.arms), 5)
        self.assertEqual(
            {arm.protocol for arm in gateway_bench.arms}, {"openai_chat"}
        )
        self.assertEqual(
            {arm.canonical_model for arm in gateway_bench.arms},
            {"moonshotai/kimi-k3"},
        )

    def test_route_reason_taxonomy_is_explicit_and_fail_closed(self):
        expected = {
            "missing_stream_metrics": "unverifiable",
            "missing_route_evidence": "unverifiable",
            "missing_requested_model": "unverifiable",
            "missing_served_model": "unverifiable",
            "stream_not_done": "unverifiable",
            "missing_gateway_profile": "unverifiable",
            "missing_cloudflare_metadata": "unverifiable",
            "missing_concentrate_metadata": "unverifiable",
            "missing_openrouter_metadata": "unverifiable",
            "missing_vercel_metadata": "unverifiable",
            "missing_provider": "unverifiable",
            "unqualified_served_model": "unverifiable",
            "missing_metadata_requested_model": "unverifiable",
            "missing_attempt_count": "unverifiable",
            "missing_model_attempts": "unverifiable",
            "missing_successful_attempt": "unverifiable",
            "missing_attempt_evidence": "unverifiable",
            "missing_attempt_provider": "unverifiable",
            "missing_attempt_model": "unverifiable",
            "missing_attempt_status": "unverifiable",
            "malformed_route_evidence": "failed",
            "malformed_events": "failed",
            "fallback_enabled": "failed",
            "served_model_conflict": "failed",
            "provider_conflict": "failed",
            "multiple_attempts": "failed",
            "served_model_not_allowed": "failed",
            "provider_not_allowed": "failed",
            "requested_model_conflict": "failed",
            "malformed_attempts": "failed",
            "fallback_attempt": "failed",
            "attempt_provider_not_allowed": "failed",
            "unsuccessful_attempt": "failed",
        }
        self.assertEqual(
            gateway_probe_http._ROUTE_REASON_STATUS,
            expected,
        )
        for reason, expected_status in expected.items():
            with self.subTest(reason=reason):
                status, reasons = gateway_probe_http._route_status({
                    "route_evidence": {
                        "pass": False,
                        "reasons": [reason],
                    }
                })
                self.assertEqual(status, expected_status)
                self.assertEqual(reasons, [reason])

        for reasons in (
            ["unknown_route_reason"],
            ["missing_provider", "provider_not_allowed"],
            [],
        ):
            with self.subTest(reasons=reasons):
                status, _normalized = gateway_probe_http._route_status({
                    "route_evidence": {
                        "pass": False,
                        "reasons": reasons,
                    }
                })
                self.assertEqual(status, "failed")

        self.assertEqual(
            gateway_probe_http._route_status({
                "route_evidence": {
                    "pass": True,
                    "reasons": ["provider_not_allowed"],
                }
            }),
            ("failed", ["provider_not_allowed"]),
        )
        self.assertEqual(
            gateway_probe_http._route_status({
                "route_evidence": {
                    "pass": True,
                    "reasons": [],
                }
            }),
            ("verified", []),
        )
        self.assertEqual(
            gateway_probe_http._route_status(None),
            ("unverifiable", ["missing_stream_metrics"]),
        )
        self.assertEqual(
            gateway_probe_http._route_status({}),
            ("unverifiable", ["missing_route_evidence"]),
        )
        self.assertEqual(
            gateway_probe_http._route_status({
                "route_evidence": {"pass": False, "reasons": "not-a-list"}
            }),
            ("failed", ["malformed_route_evidence"]),
        )

    def test_bad_status_line_never_persists_server_or_exception_text(self):
        exp = experiment(self.endpoint)
        block = ProbeBlock(
            "case", exp.cases[0].prompt_digest, "cold", 1, ("direct",)
        )
        private_server_bytes = "PRIVATE_BAD_STATUS_SERVER_BYTES"
        with mock.patch.object(
            gateway_probe_http,
            "_consume",
            side_effect=http.client.BadStatusLine(private_server_bytes),
        ):
            result = gateway_probe_http.execute_request(
                experiment=exp,
                case=exp.cases[0],
                block=block,
                plan=route_plan(exp, self.endpoint),
                secret="test-secret",
                prices={},
            )
        serialized = json.dumps(result, sort_keys=True)
        self.assertEqual(result["outcome"]["error_class"], "transport")
        self.assertEqual(
            result["outcome"]["error_detail"], "bad_status_line"
        )
        self.assertNotIn(private_server_bytes, serialized)
        self.assertNotIn("test-secret", serialized)


if __name__ == "__main__":
    unittest.main()
