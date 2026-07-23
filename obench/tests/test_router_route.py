#!/usr/bin/env python3
"""End-to-end tests for managed Router Bench proxy routes."""

import dataclasses
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

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
            event_body = {
                "model": "gpt-route-test",
                "choices": [{"delta": {"content": PRIVATE_CONTENT}}],
            }
            metadata = {
                "requested": "gpt-route-test",
                "attempts": [{
                    "provider": "openai",
                    "model": "gpt-route-test",
                    "status": 200,
                }],
                "endpoints": {
                    "available": [{"provider": "openai", "selected": True}],
                },
            }
            if self.path.startswith("/sse-wrong-provider"):
                metadata["attempts"] = []
                metadata["endpoints"] = {
                    "available": [{"provider": "different", "selected": True}],
                }
            elif self.path.startswith("/sse-no-attempts"):
                metadata.pop("attempts")
            elif self.path.startswith("/sse-fallback"):
                metadata["attempts"] = [
                    {
                        "provider": "different",
                        "model": "gpt-route-test",
                        "status": 503,
                    },
                    {
                        "provider": "openai",
                        "model": "gpt-route-test",
                        "status": 200,
                    },
                ]
            if not self.path.startswith("/sse-direct"):
                event_body["openrouter_metadata"] = metadata
            event = json.dumps(event_body, separators=(",", ":"))
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


def route_plan(
    *,
    endpoint,
    route_kind,
    arm_id,
    arm_digest,
    gateway=None,
    protocol="openai_chat",
):
    return router_spec.RoutePlan(
        schema_version=1,
        experiment_digest="e" * 64,
        arm_digest=arm_digest,
        arm_id=arm_id,
        route_kind=route_kind,
        endpoint=endpoint,
        protocol=protocol,
        canonical_model="openai/gpt-route-test",
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
        gateway=gateway or ("openrouter" if route_kind == "gateway" else None),
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

    def test_responses_route_removes_unsupported_seed(self):
        plan = route_plan(
            endpoint=self.upstream_base + "/responses",
            route_kind="direct",
            arm_id="direct-responses",
            arm_digest="d" * 64,
            protocol="openai_responses",
        )
        token = "responses-cell"
        self.server.register_cell(token)
        self.server.register_route(token, plan, secret_plan(plan.arm_id))

        status, _ = self._post(
            token,
            plan.arm_digest,
            body={
                "model": "attacker-model",
                "input": "private",
                "temperature": 0.8,
                "top_p": 0.1,
                "seed": 99,
                "prompt_cache_key": "client-cache",
                "safety_identifier": "client-safety",
            },
        )
        self.assertEqual(status, 200)
        request = self.upstream.requests[-1]
        self.assertEqual(request["path"], "/responses")
        self.assertEqual(request["body"]["model"], plan.requested_model)
        self.assertEqual(request["body"]["temperature"], 0.0)
        self.assertEqual(request["body"]["top_p"], 1.0)
        self.assertNotIn("seed", request["body"])
        self.assertNotIn("prompt_cache_key", request["body"])
        self.assertEqual(request["body"]["safety_identifier"], "client-safety")
        rows = self._seal_rows(token)
        ledger = self.server._ledger_path(token).read_text(encoding="utf-8")
        self.assertNotIn("client-cache", ledger)
        self.assertNotIn("client-safety", ledger)
        self.assertNotIn("client-safety", json.dumps(rows))

    def _register(
        self,
        token,
        *,
        route_kind="gateway",
        path="/gateway",
        gateway=None,
    ):
        digest = ("a" if route_kind == "gateway" else "b") * 64
        plan = route_plan(
            endpoint=self.upstream_base + path,
            route_kind=route_kind,
            arm_id=f"{route_kind}-{token}",
            arm_digest=digest,
            gateway=gateway,
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
                "safety_identifier": "client-safety",
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
                "x-openrouter-cache": "true",
                "x-openrouter-cache-key": "client-cache",
                "x-openrouter-cache-control": "max-age=3600",
                "accept-encoding": "gzip",
            },
        )
        self.assertEqual(status, 200)
        request = self.upstream.requests[-1]
        self.assertEqual(request["path"], "/gateway")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {HOST_SECRET}")
        self.assertEqual(request["headers"]["x-openrouter-metadata"], "enabled")
        self.assertEqual(request["headers"]["x-openrouter-cache"], "false")
        self.assertNotIn("x-openrouter-cache-key", request["headers"])
        self.assertNotIn("x-openrouter-cache-control", request["headers"])
        self.assertNotIn("x-api-key", request["headers"])
        self.assertNotIn("x-openrouter-api-key", request["headers"])
        self.assertNotIn("cookie", request["headers"])
        self.assertNotIn("x-auth-token", request["headers"])
        self.assertEqual(request["headers"].get("accept-encoding"), "identity")
        self.assertEqual(request["body"]["model"], plan.requested_model)
        self.assertEqual(request["body"]["temperature"], 0.0)
        self.assertEqual(request["body"]["top_p"], 1.0)
        self.assertEqual(request["body"]["seed"], 1234)
        self.assertNotIn("reasoning_effort", request["body"])
        self.assertNotIn("cache", request["body"])
        self.assertNotIn("prompt_cache_key", request["body"])
        self.assertEqual(request["body"]["safety_identifier"], "client-safety")
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

    def test_auto_router_session_is_stable_authoritative_and_not_persisted(self):
        token = "auto-cell"
        base = route_plan(
            endpoint=self.upstream_base + "/auto",
            route_kind="gateway",
            arm_id="auto",
            arm_digest="c" * 64,
        )
        plan = dataclasses.replace(
            base,
            track="model_router",
            router_mode="auto",
            requested_model="openrouter/auto-beta",
            requested_provider="openrouter",
            allowed_models=("openai/gpt-route-test",),
            allowed_providers=("openai",),
            fallback_enabled=True,
            cost_quality_tradeoff=7,
        )
        self.server.register_cell(token)
        self.server.register_route(token, plan, secret_plan(plan.arm_id))
        body = {
            "messages": [{"role": "user", "content": PRIVATE_PROMPT}],
            "plugins": [{"id": "attacker"}],
            "router": {"strategy": "attacker"},
            "provider": {"only": ["attacker"]},
            "session_id": "attacker-session",
            "cache": True,
        }

        for _ in range(2):
            status, _ = self._post(token, plan.arm_digest, body=body)
            self.assertEqual(status, 200)

        first, second = (request["body"] for request in self.upstream.requests)
        self.assertEqual(first["session_id"], token)
        self.assertEqual(second["session_id"], token)
        self.assertEqual(first["plugins"], [{
            "id": "auto-router",
            "allowed_models": ["openai/gpt-route-test"],
            "cost_quality_tradeoff": 7,
        }])
        self.assertEqual(first["provider"], {
            "only": ["openai"],
            "allow_fallbacks": True,
        })
        self.assertNotIn("router", first)
        self.assertNotIn("cache", first)
        rows = self._seal_rows(token)
        serialized = json.dumps(rows, sort_keys=True)
        self.assertNotIn(token, serialized)
        self.assertNotIn("attacker-session", serialized)
        self.assertNotIn("session_hash", rows[0])

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

    def test_vercel_proxy_replaces_client_routing_and_cache_controls(self):
        token = "vercel-cell"
        plan = self._register(token, gateway="vercel", path="/vercel")
        status, _ = self._post(
            token,
            plan.arm_digest,
            body={
                "model": "attacker/model",
                "messages": [{
                    "role": "user",
                    "content": PRIVATE_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                "provider": {"only": ["attacker"]},
                "providerOptions": {
                    "gateway": {
                        "models": ["fallback/model"],
                        "order": ["attacker"],
                        "caching": "auto",
                    },
                    "openai": {"reasoningEffort": "low"},
                    "attacker": {"arbitrary": {"nested": True}},
                },
                "models": ["fallback/model"],
                "order": ["attacker"],
                "sort": "price",
                "caching": "auto",
            },
        )
        self.assertEqual(status, 200)
        request = self.upstream.requests[-1]
        self.assertEqual(request["path"], "/vercel")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {HOST_SECRET}")
        self.assertNotIn("x-openrouter-metadata", request["headers"])
        self.assertEqual(
            request["body"]["providerOptions"],
            {"gateway": {"only": ["openai"]}},
        )
        for key in ("provider", "models", "order", "sort", "caching"):
            self.assertNotIn(key, request["body"])
        self.assertNotIn("cache_control", request["body"]["messages"][0])

    def test_concentrate_responses_rewrites_routing_and_isolates_secrets(self):
        token = "concentrate-cell"
        plan = dataclasses.replace(
            route_plan(
                endpoint="https://api.concentrate.ai/v1/responses",
                route_kind="gateway",
                arm_id="concentrate",
                arm_digest="c" * 64,
                gateway="concentrate",
                protocol="openai_responses",
            ),
            requested_model="openai/gpt-route-test",
            allowed_models=("openai/gpt-route-test",),
            private_router=False,
            private_host_allowlist=(),
        )
        self.server.register_cell(token)
        self.server.register_route(token, plan, secret_plan(plan.arm_id))
        fixture_host, fixture_port = self.upstream.server_address[:2]

        class FixtureHTTPSConnection(http.client.HTTPConnection):
            def __init__(self, _host, port=None, timeout=None):
                super().__init__(fixture_host, fixture_port, timeout=timeout)

        with mock.patch.object(
            proxy.http.client, "HTTPSConnection", FixtureHTTPSConnection
        ):
            statuses = []
            for call in range(2):
                status, _ = self._post(
                    token,
                    plan.arm_digest,
                    body={
                        "model": "attacker/model",
                        "input": PRIVATE_PROMPT,
                        "routing": {
                            "providers": ["attacker"],
                            "models": ["fallback/model"],
                        },
                        "fallback": {"model": "fallback/model"},
                        "prompt_cache_key": f"client-cache-{call}",
                        "prompt_cache_options": {"mode": "explicit"},
                        "cache_control": {"type": "ephemeral"},
                        "safety_identifier": f"client-safety-{call}",
                        "seed": 99,
                    },
                    headers={
                        "authorization": f"Bearer {CLIENT_SECRET}",
                        "x-api-key": CLIENT_SECRET,
                        "cookie": f"session={CLIENT_SECRET}",
                    },
                )
                statuses.append(status)

        self.assertEqual(statuses, [200, 200])
        requests = self.upstream.requests[-2:]
        for call, request in enumerate(requests):
            self.assertEqual(request["path"], "/v1/responses")
            self.assertEqual(
                request["headers"]["authorization"], f"Bearer {HOST_SECRET}"
            )
            self.assertNotIn("x-api-key", request["headers"])
            self.assertNotIn("cookie", request["headers"])
            self.assertEqual(request["body"]["model"], plan.requested_model)
            self.assertEqual(request["body"]["routing"], {
                "providers": ["openai"],
                "models": [],
            })
            for key in (
                "fallback", "prompt_cache_key", "prompt_cache_options",
                "cache_control", "seed",
            ):
                self.assertNotIn(key, request["body"])
            self.assertEqual(
                request["body"]["safety_identifier"], f"client-safety-{call}"
            )

        rows = self._seal_rows(token)
        ledger = self.server._ledger_path(token).read_text(encoding="utf-8")
        request_rows = [
            row for row in rows if row.get("record_type") != "ledger_seal"
        ]
        self.assertEqual(len(request_rows), 2)
        self.assertEqual(
            request_rows[0]["router_arm"]["arm_digest"], plan.arm_digest
        )
        private_values = (
            HOST_SECRET,
            CLIENT_SECRET,
            PRIVATE_PROMPT,
            "client-cache-0",
            "client-cache-1",
            "client-safety-0",
            "client-safety-1",
        )
        result = json.dumps(rows)
        for secret in private_values:
            self.assertNotIn(secret, ledger)
            self.assertNotIn(secret, result)

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

    def test_route_plan_constraints_drive_route_kind_aware_evidence(self):
        cases = (
            ("direct-pass", "direct", "/sse-direct", True, None),
            ("wrong-provider", "gateway", "/sse-wrong-provider", False, "provider_conflict"),
            ("no-attempts", "gateway", "/sse-no-attempts", True, None),
            ("fallback", "gateway", "/sse-fallback", False, "fallback_attempt"),
        )
        for token, route_kind, path, expected_pass, reason in cases:
            with self.subTest(token=token):
                plan = self._register(token, route_kind=route_kind, path=path)
                status, _ = self._post(token, plan.arm_digest)
                self.assertEqual(status, 200)
                evidence = self._seal_rows(token)[0]["router_metrics"]["route_evidence"]
                self.assertEqual(evidence["pass"], expected_pass)
                if reason is not None:
                    self.assertIn(reason, evidence["reasons"])


if __name__ == "__main__":
    unittest.main()
