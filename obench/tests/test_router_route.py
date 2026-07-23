#!/usr/bin/env python3
"""End-to-end tests for managed Router Bench proxy routes."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from obench import proxy
from obench import router_spec


HOST_SECRET = "HOST_ROUTER_SECRET_MUST_NOT_PERSIST"
CLIENT_SECRET = "CLIENT_CREDENTIAL_MUST_NOT_FORWARD"
PRIVATE_CONTENT = "PRIVATE_STREAM_CONTENT_MUST_NOT_PERSIST"
PRIVATE_PROMPT = "PRIVATE_PROMPT_MUST_NOT_PERSIST"


class RouteFixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RouteFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append({
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": json.loads(body),
        })
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("location", "http://example.invalid/escaped")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if self.path.startswith("/sse"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("connection", "close")
            self.end_headers()
            event = json.dumps({
                "model": "gpt-route-test",
                "openrouter_metadata": {
                    "requested": "gpt-route-test",
                    "attempts": [{
                        "provider": "openai",
                        "model": "gpt-route-test",
                        "status": 200,
                    }],
                    "endpoints": {
                        "available": [{"provider": "openai", "selected": True}],
                    },
                },
                "choices": [{"delta": {"content": PRIVATE_CONTENT}}],
            }, separators=(",", ":"))
            stream = (
                f"data: {event}\n\n"
                'data: {"usage":{"prompt_tokens":5,"completion_tokens":3,'
                '"total_tokens":8}}\n\n'
                "data: [DONE]\n\n"
            ).encode()
            cuts = (7, 19, 43, 71, 113, len(stream))
            start = 0
            for end in cuts:
                self.wfile.write(stream[start:end])
                self.wfile.flush()
                start = end
                time.sleep(0.005)
            self.close_connection = True
            return
        payload = b'{"usage":{"prompt_tokens":4,"completion_tokens":2}}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def route_plan(*, endpoint, route_kind, arm_id, arm_digest):
    return router_spec.RoutePlan(
        schema_version=1,
        experiment_digest="e" * 64,
        arm_digest=arm_digest,
        arm_id=arm_id,
        route_kind=route_kind,
        endpoint=endpoint,
        protocol="openai_chat",
        requested_model="gpt-route-test",
        requested_provider="openai",
        allowed_models=("gpt-route-test",),
        allowed_providers=("openai",),
        fallback_enabled=False,
        retry_count=0,
        cache_enabled=False,
        auth_env="ROUTER_TEST_KEY",
        sampling=router_spec.Sampling(temperature=0.0, top_p=1.0, seed=1234),
        private_router=True,
        private_host_allowlist=("127.0.0.1",),
        private_cidr_allowlist=(),
    )


def secret_plan(arm_id):
    return router_spec.SecretPlan((
        router_spec._ArmSecret(arm_id, "ROUTER_TEST_KEY", HOST_SECRET),
    ))


class RouterRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="router_route_test_")
        self.upstream = RouteFixtureServer(("127.0.0.1", 0), RouteFixtureHandler)
        self.upstream.requests = []
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.upstream_base = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.server = proxy.make_server("127.0.0.1", 0, self.tmp.name)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def _register(self, token, *, route_kind="gateway", path="/gateway"):
        digest = ("a" if route_kind == "gateway" else "b") * 64
        plan = route_plan(
            endpoint=self.upstream_base + path,
            route_kind=route_kind,
            arm_id=f"{route_kind}-{token}",
            arm_digest=digest,
        )
        self.server.register_cell(token)
        self.server.register_route(token, plan, secret_plan(plan.arm_id))
        return plan

    def _post(self, token, digest, body=None, headers=None, suffix="/client/controlled"):
        payload = json.dumps(body or {
            "model": "client-model",
            "messages": [{"role": "user", "content": PRIVATE_PROMPT}],
            "temperature": 0.9,
            "top_p": 0.2,
            "seed": 9,
        }).encode()
        request_headers = {
            "content-type": "application/json",
            "content-length": str(len(payload)),
        }
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request(
            "POST",
            f"/cell/{token}/route/{digest}{suffix}?client=query",
            body=payload,
            headers=request_headers,
        )
        response = conn.getresponse()
        response_body = response.read()
        conn.close()
        return response.status, response_body

    def _seal_rows(self, token):
        seal = self.server.seal_cell(token, timeout_s=2)
        with seal.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_gateway_authorizes_exact_arm_and_rewrites_credentials_and_body(self):
        token = "gateway-cell"
        plan = self._register(token)
        status, _ = self._post(token, "f" * 64)
        self.assertEqual(status, 502)
        self.assertEqual(self.upstream.requests, [])

        status, _ = self._post(
            token,
            plan.arm_digest,
            body={
                "model": "attacker-model",
                "messages": [{
                    "role": "user",
                    "content": PRIVATE_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                "provider": {"only": ["attacker"], "allow_fallbacks": True},
                "cache": True,
                "prompt_cache_key": "client-cache",
                "temperature": 0.8,
                "top_p": 0.1,
                "seed": 99,
                "reasoning_effort": "high",
            },
            headers={
                "authorization": f"Bearer {CLIENT_SECRET}",
                "x-api-key": CLIENT_SECRET,
                "x-openrouter-api-key": CLIENT_SECRET,
                "cookie": f"session={CLIENT_SECRET}",
                "x-auth-token": CLIENT_SECRET,
                "x-openrouter-metadata": "false",
            },
        )
        self.assertEqual(status, 200)
        request = self.upstream.requests[-1]
        self.assertEqual(request["path"], "/gateway")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {HOST_SECRET}")
        self.assertEqual(request["headers"]["x-openrouter-metadata"], "enabled")
        self.assertNotIn("x-api-key", request["headers"])
        self.assertNotIn("x-openrouter-api-key", request["headers"])
        self.assertNotIn("cookie", request["headers"])
        self.assertNotIn("x-auth-token", request["headers"])
        self.assertEqual(request["body"]["model"], plan.requested_model)
        self.assertEqual(request["body"]["temperature"], 0.0)
        self.assertEqual(request["body"]["top_p"], 1.0)
        self.assertEqual(request["body"]["seed"], 1234)
        self.assertNotIn("reasoning_effort", request["body"])
        self.assertNotIn("cache", request["body"])
        self.assertNotIn("prompt_cache_key", request["body"])
        self.assertNotIn("cache_control", request["body"]["messages"][0])
        self.assertEqual(request["body"]["provider"], {
            "only": ["openai"],
            "allow_fallbacks": False,
        })

        rows = self._seal_rows(token)
        ledger = self.server._ledger_path(token).read_text(encoding="utf-8")
        self.assertEqual(rows[0]["router_arm"]["arm_digest"], plan.arm_digest)
        for secret in (HOST_SECRET, CLIENT_SECRET, PRIVATE_PROMPT):
            self.assertNotIn(secret, ledger)

    def test_direct_openai_forces_model_and_sampling_without_gateway_metadata(self):
        token = "direct-cell"
        plan = self._register(token, route_kind="direct", path="/direct")
        status, _ = self._post(
            token,
            plan.arm_digest,
            body={
                "model": "wrong",
                "messages": [],
                "provider": {"only": ["wrong"]},
                "temperature": 1.0,
                "top_p": 0.0,
                "seed": 1,
            },
        )
        self.assertEqual(status, 200)
        request = self.upstream.requests[-1]
        self.assertEqual(request["path"], "/direct")
        self.assertNotIn("x-openrouter-metadata", request["headers"])
        self.assertNotIn("provider", request["body"])
        self.assertEqual(request["body"]["model"], plan.requested_model)
        self.assertEqual(
            {key: request["body"][key] for key in ("temperature", "top_p", "seed")},
            {"temperature": 0.0, "top_p": 1.0, "seed": 1234},
        )

    def test_redirect_is_rejected_and_recorded_without_following(self):
        token = "redirect-cell"
        plan = self._register(token, path="/redirect")
        status, _ = self._post(token, plan.arm_digest)
        self.assertEqual(status, 502)
        self.assertEqual(len(self.upstream.requests), 1)
        rows = self._seal_rows(token)
        self.assertEqual(rows[0]["status"], 502)
        self.assertIn("redirect rejected", rows[0]["error"])

    def test_fragmented_sse_adds_privacy_safe_metrics_to_sealed_ledger(self):
        token = "stream-cell"
        plan = self._register(token, path="/sse")
        status, response = self._post(token, plan.arm_digest)
        self.assertEqual(status, 200)
        self.assertIn(PRIVATE_CONTENT.encode(), response)

        rows = self._seal_rows(token)
        request = rows[0]
        metrics = request["router_metrics"]
        self.assertEqual(metrics["usage"], {
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
        })
        self.assertEqual(metrics["route"]["requested_model"], "gpt-route-test")
        self.assertEqual(metrics["route"]["served_model"], "gpt-route-test")
        self.assertEqual(metrics["route"]["provider"], "openai")
        self.assertTrue(metrics["route_evidence"]["pass"])
        self.assertTrue(metrics["stream"]["done"])
        self.assertTrue(metrics["stream"]["finalized"])
        self.assertGreaterEqual(metrics["timing"]["ttfb_s"], 0)
        self.assertGreaterEqual(metrics["timing"]["semantic_ttft_s"], 0)
        self.assertGreaterEqual(metrics["timing"]["total_s"], 0)
        self.assertEqual(request["router_arm"]["arm_digest"], plan.arm_digest)

        ledger = self.server._ledger_path(token).read_text(encoding="utf-8")
        for secret in (HOST_SECRET, CLIENT_SECRET, PRIVATE_CONTENT, PRIVATE_PROMPT):
            self.assertNotIn(secret, ledger)


if __name__ == "__main__":
    unittest.main()
