"""Deterministic Harbor-to-CountingProxy metering bridge tests."""

from __future__ import annotations

import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from obench import harbor_metering


class _UsageUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(size)
        self.server.observed.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("authorization"),
                "account": self.headers.get("chatgpt-account-id"),
                "originator": self.headers.get("originator"),
                "body": body,
            }
        )
        payload = json.dumps(self.server.response_payload).encode()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


class HarborMeteringSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="obench_harbor_metering_")
        self.upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0), _UsageUpstreamHandler
        )
        self.upstream.observed = []
        self.upstream.response_payload = {
            "usage": {
                "input_tokens": 12,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens": 4,
            }
        }
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)
        self.temp.cleanup()

    def _session(self, name="trial-001"):
        session = harbor_metering.HarborMeteringSession(
            Path(self.temp.name) / name,
            name,
            listen_host="127.0.0.1",
            advertised_host="127.0.0.1",
        )
        session.server.upstreams["codex"] = urlsplit(
            f"http://127.0.0.1:{self.upstream.server_address[1]}"
        )
        return session

    @staticmethod
    def _post_model_call(session):
        endpoint = urlsplit(
            session.process_env({})[
                harbor_metering.HARBOR_BASE_URL_SOURCE_ENV
            ]
            + "/responses"
        )
        body = b'{"model":"gpt-test","input":"RAW-PROMPT-MUST-NOT-PERSIST"}'
        connection = http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=5
        )
        connection.request(
            "POST",
            endpoint.path,
            body=body,
            headers={
                "authorization": "Bearer OAUTH-SECRET-MUST-NOT-PERSIST",
                "chatgpt-account-id": "account-123",
                "originator": "codex_cli_rs",
                "content-type": "application/json",
                "content-length": str(len(body)),
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def test_secure_setup_normal_route_exact_seal_and_teardown(self):
        evidence_root = Path(self.temp.name) / "exact"
        with self._session("exact") as session:
            self.assertEqual(
                session.agent_env,
                {
                    "OPENAI_BASE_URL":
                        "${OPENBENCH_HARBOR_METERING_BASE_URL}"
                },
            )
            endpoint = session.process_env({})[
                harbor_metering.HARBOR_BASE_URL_SOURCE_ENV
            ]
            self.assertIn("/codex/backend-api/codex", endpoint)
            self.assertEqual(self._post_model_call(session), 200)
            evidence = session.seal(
                harbor_metering.UsageCounters(1, 12, 3, 4)
            )
            thread = session._thread

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            self.upstream.observed[0]["path"],
            "/backend-api/codex/responses",
        )
        self.assertEqual(
            self.upstream.observed[0]["authorization"],
            "Bearer OAUTH-SECRET-MUST-NOT-PERSIST",
        )
        self.assertEqual(self.upstream.observed[0]["account"], "account-123")
        self.assertEqual(self.upstream.observed[0]["originator"], "codex_cli_rs")

        self.assertEqual(evidence["reconciliation"]["status"], "exact")
        self.assertTrue(evidence["proxy_complete"])
        self.assertTrue(evidence["publication"]["eligible"])
        self.assertFalse(evidence["transport"]["tls_interception"])
        self.assertEqual(
            evidence["proxy_measured"],
            {
                "calls": 1,
                "input_tokens": 12,
                "cache_tokens": 3,
                "output_tokens": 4,
            },
        )

        public_bytes = (evidence_root / "harbor-metering.json").read_bytes()
        private_bytes = b"".join(
            path.read_bytes() for path in (evidence_root / "private").iterdir()
        )
        for secret in (
            b"RAW-PROMPT-MUST-NOT-PERSIST",
            b"OAUTH-SECRET-MUST-NOT-PERSIST",
            b"account-123",
        ):
            self.assertNotIn(secret, public_bytes)
            self.assertNotIn(secret, private_bytes)
        self.assertNotIn(
            urlsplit(endpoint).path.encode(),
            public_bytes,
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(evidence_root).st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(evidence_root / "private").st_mode),
            0o700,
        )
        for path in [evidence_root / "harbor-metering.json"] + list(
            (evidence_root / "private").iterdir()
        ):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_mismatch_is_machine_visible_and_required_evidence_blocks(self):
        with self._session("mismatch") as session:
            self._post_model_call(session)
            evidence = session.seal(
                harbor_metering.UsageCounters(1, 13, 3, 4)
            )

        self.assertEqual(evidence["reconciliation"]["status"], "mismatch")
        self.assertEqual(
            evidence["reconciliation"]["fields"]["input_tokens"]["status"],
            "mismatch",
        )
        self.assertFalse(evidence["publication"]["eligible"])
        with self.assertRaisesRegex(
            harbor_metering.HarborMeteringPublicationError,
            "proxy_evidence_mismatch",
        ):
            harbor_metering.require_publication_eligible(
                evidence, proxy_required=True
            )
        harbor_metering.require_publication_eligible(
            evidence, proxy_required=False
        )

    def test_missing_usage_is_incomplete_and_required_evidence_blocks(self):
        self.upstream.response_payload = {"id": "response-without-usage"}
        with self._session("incomplete") as session:
            self._post_model_call(session)
            evidence = session.seal(
                harbor_metering.UsageCounters(1, 12, 3, 4)
            )

        self.assertEqual(evidence["reconciliation"]["status"], "incomplete")
        self.assertFalse(evidence["proxy_complete"])
        self.assertIn("request_1_usage_incomplete", evidence["errors"])
        self.assertEqual(evidence["proxy_measured"]["calls"], 1)
        self.assertIsNone(evidence["proxy_measured"]["input_tokens"])
        self.assertEqual(
            evidence["publication"]["blocking_reasons"],
            ["proxy_evidence_incomplete"],
        )

    def test_context_teardown_runs_on_exception(self):
        session = self._session("exception")
        thread = session._thread
        with self.assertRaisesRegex(RuntimeError, "trial failed"):
            with session:
                raise RuntimeError("trial failed")
        self.assertFalse(thread.is_alive())
        with self.assertRaisesRegex(
            harbor_metering.HarborMeteringError, "closed"
        ):
            _ = session.agent_env


class HarborMeteringContractTests(unittest.TestCase):
    def test_reconciliation_missing_agent_counter_is_incomplete(self):
        result = harbor_metering.reconcile_usage(
            harbor_metering.UsageCounters(None, 10, 2, 3),
            harbor_metering.UsageCounters(1, 10, 2, 3),
            proxy_complete=True,
        )
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.fields["calls"]["status"], "incomplete")

    def test_usage_from_imported_harbor_row_and_integrator_hook(self):
        row = {
            "turns": 2,
            "tokens_input_uncached": 80,
            "tokens_cache_read": 20,
            "tokens_output": 7,
            "candidate_provenance": {"kind": "harbor_job"},
        }
        usage = harbor_metering.UsageCounters.from_openbench_row(row)
        self.assertEqual(
            usage,
            harbor_metering.UsageCounters(2, 100, 20, 7),
        )
        evidence = {
            "schema_version": harbor_metering.SCHEMA_VERSION,
            "proxy_complete": True,
            "proxy_measured": {
                "calls": 2,
                "input_tokens": 100,
                "cache_tokens": 20,
                "output_tokens": 7,
            },
            "ledger_seal": {"root_hash": "abc"},
            "reconciliation": {"status": "exact"},
        }
        updated = harbor_metering.apply_to_imported_row(
            row, evidence, proxy_required=True
        )
        self.assertEqual(updated["tokens_proxy_calls"], 2)
        self.assertEqual(updated["tokens_proxy_input_uncached"], 80)
        self.assertEqual(updated["token_basis_proxy"], "proxy_measured")
        gate = updated["candidate_provenance"]["harbor_metering"]["publication"]
        self.assertTrue(gate["eligible"])

    def test_loader_rejects_symlink_and_invalid_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = root / "invalid.json"
            invalid.write_text(
                json.dumps(
                    {
                        "schema_version": harbor_metering.SCHEMA_VERSION,
                        "reconciliation": {"status": "unknown"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                harbor_metering.HarborMeteringError, "reconciliation"
            ):
                harbor_metering.load_evidence(invalid)

            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(
                harbor_metering.HarborMeteringError, "regular file"
            ):
                harbor_metering.load_evidence(link)

    def test_invalid_evidence_fails_closed_when_proxy_is_required(self):
        decision = harbor_metering.publication_decision(
            {}, proxy_required=True
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(
            decision["blocking_reasons"], ["invalid_metering_evidence"]
        )


if __name__ == "__main__":
    unittest.main()
