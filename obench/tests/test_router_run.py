"""Focused local Gateway Tax runner and CLI integration tests."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from obench import cli, proxy, results, router_report, router_run


SECRET = "gateway-tax-secret-that-must-not-persist"
PRICE_JSON = json.dumps({
    "openai/fake-model": {
        "input_per_million": "1.00",
        "output_per_million": "2.00",
        "effective_at": "2026-07-22T00:00:00Z",
    }
})


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        self.server.requests.append({  # type: ignore[attr-defined]
            "path": self.path,
            "body": body,
            "authorization": self.headers.get("authorization"),
        })
        gateway = "/gateway/" in self.path
        status = 503 if gateway else 200
        event = {
            "model": "fake-model",
            "provider": "openai",
            "openrouter_metadata": {
                "requested": "fake-model",
                "attempts": [{
                    "provider": "openai",
                    "model": "fake-model",
                    "status": status,
                }],
                "endpoints": {
                    "available": [{"provider": "openai", "selected": True}]
                },
            },
            "choices": [{"delta": {"content": "fake completion"}}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        }
        payload = (
            f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            "data: [DONE]\n\n"
        ).encode()
        self.send_response(status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


FAKE_ADAPTER = '''\
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ADAPTER_API_VERSION = 2
ROUTED_CAPABILITIES = {
    "protocols": ["openai_chat"],
    "execution_lanes": ["local", "docker"],
    "streaming": True,
    "dynamic_model_ids": True,
    "route_plan_transport": "sanitized_file",
}

def run(*_args):
    raise AssertionError("ordinary run() must not be used")

def run_routed(_instruction, workdir, route_plan_path, _timeout_s):
    plan = json.loads(Path(route_plan_path).read_text())
    base = os.environ["OPENBENCH_PROXY_BASE_URL"]
    token = os.environ["OPENBENCH_PROXY_CELL_TOKEN"]
    url = urlsplit(base)
    body = json.dumps({
        "model": "client-controlled",
        "messages": [{"role": "user", "content": "private prompt"}],
        "stream": True,
        "temperature": 1.5,
        "top_p": 0.1,
        "seed": 999,
    })
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    conn.request(
        "POST",
        f"/cell/{token}/route/{plan['arm_digest']}",
        body=body,
        headers={
            "content-type": "application/json",
            "authorization": "Bearer client-secret",
        },
    )
    response = conn.getresponse()
    response.read()
    conn.close()
    if plan["route_kind"] == "direct":
        Path(workdir, "solved.txt").write_text("checker owns the verdict\\n")
        return {"completed": False, "error": "adapter self-report says failure"}
    return {"completed": True, "error": None}
'''


class RouterRunE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router_run_test_")
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks"
        self.task = self.tasks / "fake-task"
        (self.task / "workspace").mkdir(parents=True)
        (self.task / "workspace" / "README.txt").write_text("fresh\n")
        (self.task / "instruction.md").write_text("Create solved.txt.\n")
        checker = self.task / "checker.sh"
        checker.write_text(
            '#!/usr/bin/env bash\n'
            'test -z "${DIRECT_KEY:-}"\n'
            'test -z "${GATEWAY_KEY:-}"\n'
            'test "$(cat solved.txt 2>/dev/null)" = "checker owns the verdict"\n'
        )
        checker.chmod(0o755)

        self.adapters = self.root / "adapters"
        self.adapters.mkdir()
        (self.adapters / "pi.py").write_text(FAKE_ADAPTER)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.upstream.requests = []
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        port = self.upstream.server_address[1]
        self.experiment = self.root / "experiment.toml"
        self.experiment.write_text(self._experiment_toml(port))
        self.results = self.root / "router-results.jsonl"
        self.env = {
            "DIRECT_KEY": SECRET,
            "GATEWAY_KEY": SECRET,
            router_run.FROZEN_PRICES_ENV: PRICE_JSON,
            router_run.ADAPTERS_DIR_ENV: str(self.adapters),
        }

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)
        self.temp.cleanup()

    @staticmethod
    def _experiment_toml(port):
        common = '''\
protocol = "openai_chat"
canonical_model = "openai/fake-model"
requested_model = "fake-model"
requested_provider = "openai"
allowed_models = ["fake-model"]
allowed_providers = ["openai"]
fallback_enabled = false
retry_count = 0
cache_enabled = false

[arms.sampling]
temperature = 0.0
top_p = 1.0
seed = 7
'''
        return f'''\
schema_version = 1
experiment_id = "fake-gateway-tax"
track = "gateway_tax"
harness = "pi"
tasks = ["fake-task"]
repetitions_per_window = 1
schedule_seed = 11
execution_lane = "local"
private_router = true
private_cidr_allowlist = ["127.0.0.1/32"]

[[windows]]
window_id = "w1"
start = "2026-07-01T00:00:00Z"
end = "2026-08-01T00:00:00Z"

[budget]
timeout_s = 10
max_calls = 1
max_output_tokens = 10
usd_cap = "1.0"

[[arms]]
arm_id = "direct"
route_kind = "direct"
endpoint = "https://127.0.0.1:{port}/direct/v1/chat/completions"
baseline = true
auth_env = "DIRECT_KEY"
{common}

[[arms]]
arm_id = "gateway"
route_kind = "gateway"
endpoint = "https://127.0.0.1:{port}/gateway/v1/chat/completions"
baseline = false
auth_env = "GATEWAY_KEY"
direct_control_arm_id = "direct"
{common}
'''

    @contextlib.contextmanager
    def _runtime(self):
        # The schema correctly requires HTTPS. The in-process fake transport
        # speaks plain HTTP; replace only the proxy client's connection class.
        class PlainHTTPSConnection(http.client.HTTPConnection):
            pass

        with (
            mock.patch.object(proxy.http.client, "HTTPSConnection", PlainHTTPSConnection),
            mock.patch.object(router_run.pi, "version", return_value="fake-pi 1.0"),
            mock.patch.dict(os.environ, self.env, clear=False),
        ):
            yield

    def _cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_local_cli_vertical_slice_resume_seal_and_treatment_denominator(self):
        with self._runtime():
            code, out, err = self._cli([
                "router", "validate", str(self.experiment),
                "--tasks-dir", str(self.tasks),
            ])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("valid experiment=fake-gateway-tax", out)

            code, out, err = self._cli([
                "router", "doctor", str(self.experiment),
                "--tasks-dir", str(self.tasks),
            ])
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(json.loads(out)["usd_cap_enforceable"])

            code, out, err = self._cli([
                "router", "run", str(self.experiment),
                "--results", str(self.results),
                "--tasks-dir", str(self.tasks),
                "--exec", "local",
            ])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("rows_appended=2", out)
            first_rows = results.read_jsonl_for_resume(self.results).rows
            self.assertEqual({row["arm_role"] for row in first_rows}, {"direct", "gateway"})
            self.assertEqual({tuple(row["schedule_order"]) for row in first_rows},
                             {tuple(first_rows[0]["schedule_order"])})
            self.assertTrue(all(row["ledger_seal"]["record_count"] == 1
                                for row in first_rows))
            self.assertTrue(all(row["route_integrity"]["pass"] for row in first_rows))
            self.assertTrue(all(
                row["result"]["infrastructure_invalid_reason"] is None
                for row in first_rows
            ))
            direct = next(row for row in first_rows if row["arm_role"] == "direct")
            gateway = next(row for row in first_rows if row["arm_role"] == "gateway")
            self.assertTrue(direct["result"]["solved"])
            self.assertFalse(direct["result"]["adapter_completed"])
            self.assertFalse(gateway["result"]["solved"])
            self.assertTrue(gateway["result"]["adapter_completed"])
            self.assertFalse(gateway["result"]["available"])
            self.assertEqual(
                router_report.aggregate(first_rows)["arms"]["gateway"]["attempted_cells"],
                1,
            )

            bundle = self.root / "bundle"
            self.assertTrue(
                (self.root / ".router-results.router-prices.json").is_file()
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                code, out, err = self._cli([
                    "router", "publish", str(self.results), str(self.experiment), str(bundle),
                ])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["result_count"], 2)
            code, out, err = self._cli(["router", "verify", str(bundle)])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["result_count"], 2)
            published = results.read_jsonl_for_resume(bundle / "results.jsonl").rows
            self.assertEqual(
                {row["route_isolation"]["classification"] for row in published},
                {"exploratory"},
            )

            # A fully valid block is skipped on resume.
            resumed = router_run.run_experiment(
                self.experiment,
                results_path=self.results,
                tasks_dir=self.tasks,
                exec_mode="local",
                environ={**os.environ, **self.env},
                adapters_dir=self.adapters,
            )
            self.assertEqual((resumed.rows_appended, resumed.blocks_skipped), (0, 1))

            # A partial attempt is preserved; resume starts a fresh all-arm attempt.
            partial = self.root / "partial.jsonl"
            partial.write_text(json.dumps(first_rows[0], sort_keys=True) + "\n")
            replacement = router_run.run_experiment(
                self.experiment,
                results_path=partial,
                tasks_dir=self.tasks,
                exec_mode="local",
                environ={**os.environ, **self.env},
                adapters_dir=self.adapters,
            )
            self.assertEqual(replacement.rows_appended, 2)
            replacement_rows = results.read_jsonl_for_resume(partial).rows
            self.assertEqual(
                [row["identity"]["schedule"]["block_attempt"] for row in replacement_rows],
                [0, 1, 1],
            )

            code, out, err = self._cli(["router", "report", str(partial), "--json"])
            self.assertEqual((code, err), (0, ""))
            report = json.loads(out)
            self.assertEqual(report["blocks"], {
                "excluded": 0,
                "excluded_by_reason": {},
                "included": 1,
                "observed": 1,
            })
            self.assertEqual(report["arms"]["gateway"]["attempted_cells"], 1)

        persisted = self.results.read_text()
        ledgers = "".join(
            path.read_text()
            for path in (self.root / ".router-results.router-ledgers").glob("*.jsonl")
        )
        self.assertNotIn(SECRET, persisted + ledgers)
        self.assertEqual(
            {request["authorization"] for request in self.upstream.requests},
            {f"Bearer {SECRET}"},
        )

    def test_active_schedule_respects_declared_window(self):
        experiment = router_run.router_spec.load_experiment(self.experiment)
        schedule = router_run.build_schedule(experiment)
        inside = router_run.datetime.fromisoformat("2026-07-15T00:00:00+00:00")
        outside = router_run.datetime.fromisoformat("2026-08-02T00:00:00+00:00")
        self.assertEqual(len(router_run._active_schedule(experiment, schedule, inside)), 1)
        self.assertEqual(router_run._active_schedule(experiment, schedule, outside), ())

    def test_budget_violation_never_retries_paid_block_automatically(self):
        limited = self.root / "limited.toml"
        limited.write_text(
            self.experiment.read_text().replace("max_output_tokens = 10", "max_output_tokens = 1")
        )
        limited_results = self.root / "limited.jsonl"
        with self._runtime():
            with self.assertRaisesRegex(router_run.RouterRunError, "explicit rerun"):
                router_run.run_experiment(
                    limited,
                    results_path=limited_results,
                    tasks_dir=self.tasks,
                    exec_mode="local",
                    environ={**os.environ, **self.env},
                    adapters_dir=self.adapters,
                )
        rows = results.read_jsonl_for_resume(limited_results).rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["identity"]["schedule"]["block_attempt"] for row in rows},
            {0},
        )

    def test_proxy_evidence_honors_parser_integrity_verdict(self):
        experiment = router_run.router_spec.load_experiment(self.experiment)
        plans, _secrets = router_run.router_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = next(item for item in plans if item.route_kind == "gateway")
        metrics = {
            "route": {
                "requested_model": plan.requested_model,
                "metadata_requested_model": plan.requested_model,
                "served_model": plan.requested_model,
                "provider": plan.requested_provider,
                "attempts": [],
            },
            "stream": {"done": True},
            "route_evidence": {
                "pass": False,
                "reasons": ["malformed_sse_event"],
            },
        }
        self.assertIn("malformed_sse_event", router_run._route_reasons(metrics, plan))

    def test_docker_fails_closed_in_local_mvp(self):
        with self._runtime():
            with self.assertRaisesRegex(router_run.RouterRunError, "unsupported"):
                router_run.run_experiment(
                    self.experiment,
                    results_path=self.results,
                    tasks_dir=self.tasks,
                    exec_mode="docker",
                    environ={**os.environ, **self.env},
                    adapters_dir=self.adapters,
                )


if __name__ == "__main__":
    unittest.main()
