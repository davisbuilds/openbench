import dataclasses
import http.client
import json
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from obench import (
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

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        size = int(self.headers["content-length"])
        payload = json.loads(self.rfile.read(size))
        type(self).requests.append({
            "connection": id(self.connection),
            "payload": payload,
        })
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
        close_response = self.close_measured and len(self.requests) == 2
        self.send_header(
            "connection", "close" if close_response else "keep-alive"
        )
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


def experiment(endpoint):
    arm = gateway_spec.Arm(
        arm_id="direct",
        route_kind="direct",
        endpoint=endpoint,
        protocol="openai_responses",
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
        protocol="openai_responses",
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
            result["reuse_evidence"]["connection"]["dns_s"]
        )
        self.assertEqual(
            result["request_metrics"]["connection"],
            {"dns_s": None, "tcp_s": None, "tls_s": None},
        )
        self.assertGreater(
            Decimal(result["billing"]["primer_cost_usd"]), 0
        )
        self.assertGreater(
            Decimal(result["billing"]["charged_cost_usd"]),
            Decimal(result["billing"]["measured_cost_usd"]),
        )

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
