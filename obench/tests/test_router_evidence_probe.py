import copy
import io
import unittest

from obench import router_evidence_probe as probe


class RouterEvidenceProbeTests(unittest.TestCase):
    def test_parse_sse_returns_terminal_response(self):
        response = {
            "id": "gen-1",
            "model": "vendor/model",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        }
        stream = (
            'data: {"type":"response.created"}\n\n'
            f'data: {{"type":"response.completed","response":'
            f'{probe._canonical_json(response)}}}\n\n'
            "data: [DONE]\n\n"
        )
        terminal, events = probe.parse_sse_lines(io.BytesIO(stream.encode()))
        self.assertEqual(terminal["id"], "gen-1")
        self.assertEqual(events, ["response.created", "response.completed"])

    def test_openrouter_reconciles_response_metadata_and_trace(self):
        response = {
            "id": "gen-1",
            "model": "deepseek/deepseek-v4-flash",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "cost": 0.001,
            },
            "openrouter_metadata": {
                "requested": "openrouter/auto-beta",
                "attempt": 1,
                "endpoints": {
                    "total": 2,
                    "available": [{
                        "provider": "Baidu",
                        "model": "deepseek/deepseek-v4-flash-20260423",
                        "selected": True,
                    }, {
                        "provider": "Other",
                        "model": "vendor/other-model-20260101",
                        "selected": False,
                    }],
                },
                "pipeline": [{
                    "type": "plugin",
                    "name": "auto-beta-router",
                    "data": {
                        "resolved_to": "deepseek/deepseek-v4-flash-20260423",
                        "fallback_models": ["vendor/other-model-20260101"],
                    },
                }],
            },
        }
        trace = {
            "id": "gen-1",
            "request_id": "req-1",
            "model": "deepseek/deepseek-v4-flash-20260423",
            "provider": "Baidu",
            "router": "openrouter/auto-beta",
            "total_cost": 0.001,
            "provider_responses": [{
                "model": "deepseek/deepseek-v4-flash-20260423",
                "provider": "Baidu",
                "status": 200,
            }],
        }
        result = probe.reconcile_openrouter(
            requested_router="openrouter/auto-beta",
            response=response,
            response_headers={"x-generation-id": "gen-1"},
            trace=trace,
        )
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["failures"], [])

    def test_openrouter_provider_mismatch_fails_closed(self):
        response = {
            "id": "gen-1",
            "model": "vendor/model",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
            "openrouter_metadata": {
                "requested": "openrouter/auto-beta",
                "endpoints": {
                    "total": 2,
                    "available": [{
                        "provider": "Provider A",
                        "model": "vendor/model-20260101",
                        "selected": True,
                    }, {
                        "provider": "Other",
                        "model": "other/model-20260101",
                        "selected": False,
                    }],
                },
                "pipeline": [{
                    "name": "auto-beta-router",
                    "data": {"resolved_to": "vendor/model-20260101"},
                }],
            },
        }
        result = probe.reconcile_openrouter(
            requested_router="openrouter/auto-beta",
            response=response,
            response_headers={},
            trace={
                "id": "gen-1",
                "request_id": "req-1",
                "model": "vendor/model-20260101",
                "provider": "Provider B",
                "router": "openrouter/auto-beta",
                "provider_responses": [{
                    "model": "vendor/model-20260101",
                    "provider": "Provider B",
                    "status": 200,
                }],
            },
        )
        self.assertEqual(result["status"], "unverifiable")
        self.assertIn("trace_provider_matches_selected", result["failures"])

    def test_concentrate_is_observed_not_reconciled(self):
        result = probe.reconcile_concentrate({
            "id": "msg-1",
            "model": "anthropic/claude-opus",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        })
        self.assertEqual(result["status"], "observed")
        self.assertFalse(result["checks"]["trace_api_available"])

    def test_artifact_digest_detects_tampering(self):
        artifact = {
            "schema_version": 1,
            "kind": "router_evidence_probe",
            "records": [{
                "reconciliation": {"status": "observed"},
            }],
        }
        artifact["artifact_sha256"] = probe._digest(artifact)
        self.assertTrue(probe.verify_artifact(artifact)["ok"])
        tampered = copy.deepcopy(artifact)
        tampered["records"][0]["reconciliation"]["status"] = "reconciled"
        result = probe.verify_artifact(tampered)
        self.assertFalse(result["ok"])
        self.assertIn("artifact_sha256", result["failures"])

    def test_artifact_rejects_raw_output_fields(self):
        artifact = {
            "schema_version": 1,
            "kind": "router_evidence_probe",
            "records": [{
                "output": "secret model response",
                "reconciliation": {"status": "observed"},
            }],
        }
        artifact["artifact_sha256"] = probe._digest(artifact)
        result = probe.verify_artifact(artifact)
        self.assertFalse(result["ok"])
        self.assertIn("privacy_safe", result["failures"])


if __name__ == "__main__":
    unittest.main()
