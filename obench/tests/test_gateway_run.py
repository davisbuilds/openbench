"""Focused local Gateway Bench runner and CLI integration tests."""

from __future__ import annotations

import contextlib
import dataclasses
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from obench import (
    cli,
    gateway_metrics,
    gateway_probe_publish,
    gateway_report,
    gateway_run,
    proxy,
    results,
)


SECRET = "gateway-bench-secret-that-must-not-persist"
PRICE_JSON = json.dumps({
    "openai/fake-model": {
        "input_per_million": "1.00",
        "output_per_million": "2.00",
        "effective_at": "2026-07-22T00:00:00Z",
    }
})
PROVIDER_DEFAULT_EVIDENCE = {
    "mode": "provider_default",
    "transform_id": None,
    "prefix_injected": False,
    "scope": None,
    "nonce_commitment": None,
}


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
        status = self.server.gateway_status if gateway else 200
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


class GatewayRunE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway_run_test_")
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
        self.upstream.gateway_status = 503
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        port = self.upstream.server_address[1]
        self.experiment = self.root / "experiment.toml"
        self.experiment.write_text(self._experiment_toml(port))
        self.results = self.root / "gateway-results.jsonl"
        self.env = {
            "DIRECT_KEY": SECRET,
            "GATEWAY_KEY": SECRET,
            gateway_run.FROZEN_PRICES_ENV: PRICE_JSON,
            gateway_run.ADAPTERS_DIR_ENV: str(self.adapters),
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
schema_version = 2
experiment_id = "fake-gateway-bench"
track = "fixed_model_provider"
provider_prompt_mode = "provider_default"
harness = "pi"
tasks = ["fake-task"]
repetitions_per_window = 1
schedule_seed = 11
execution_lane = "local"
allow_private_endpoint = true
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
gateway = "openrouter"
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

        class FrozenDateTime(gateway_run.datetime):
            @classmethod
            def now(cls, tz=None):
                fixed = cls.fromisoformat("2026-07-15T00:00:00+00:00")
                return fixed if tz is None else fixed.astimezone(tz)

        with (
            mock.patch.object(proxy.http.client, "HTTPSConnection", PlainHTTPSConnection),
            mock.patch.object(gateway_run, "datetime", FrozenDateTime),
            mock.patch.object(gateway_run.pi, "version", return_value="fake-pi 1.0"),
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
                "gateway", "validate", str(self.experiment),
                "--tasks-dir", str(self.tasks),
            ])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("valid experiment=fake-gateway-bench", out)

            code, out, err = self._cli([
                "gateway", "doctor", str(self.experiment),
                "--tasks-dir", str(self.tasks),
            ])
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(json.loads(out)["usd_cap_enforceable"])

            code, out, err = self._cli([
                "gateway", "run", str(self.experiment),
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
                gateway_report.aggregate(first_rows)["arms"]["gateway"]["attempted_cells"],
                1,
            )

            bundle = self.root / "bundle"
            self.assertTrue(
                (self.root / ".gateway-results.gateway-prices.json").is_file()
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                code, out, err = self._cli([
                    "gateway", "publish", str(self.results), str(self.experiment), str(bundle),
                ])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["result_count"], 2)
            code, out, err = self._cli(["gateway", "verify", str(bundle)])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["result_count"], 2)
            published = results.read_jsonl_for_resume(bundle / "results.jsonl").rows
            self.assertEqual(
                {row["route_isolation"]["classification"] for row in published},
                {"exploratory"},
            )

            # A fully valid block is skipped on resume.
            resumed = gateway_run.run_experiment(
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
            replacement = gateway_run.run_experiment(
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

            code, out, err = self._cli(["gateway", "report", str(partial), "--json"])
            self.assertEqual((code, err), (0, ""))
            report = json.loads(out)
            self.assertEqual(report["blocks"], {
                "excluded": 0,
                "excluded_by_reason": {},
                "included": 1,
                "max_calls_affected": 0,
                "max_calls_rate": 0.0,
                "observed": 1,
            })
            self.assertEqual(report["arms"]["gateway"]["attempted_cells"], 1)

        persisted = self.results.read_text()
        ledgers = "".join(
            path.read_text()
            for path in (self.root / ".gateway-results.gateway-ledgers").glob("*.jsonl")
        )
        self.assertNotIn(SECRET, persisted + ledgers)
        self.assertEqual(
            {request["authorization"] for request in self.upstream.requests},
            {f"Bearer {SECRET}"},
        )

    def test_active_schedule_respects_declared_window(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        schedule = gateway_run.build_schedule(experiment)
        inside = gateway_run.datetime.fromisoformat("2026-07-15T00:00:00+00:00")
        outside = gateway_run.datetime.fromisoformat("2026-08-02T00:00:00+00:00")
        self.assertEqual(len(gateway_run._active_schedule(experiment, schedule, inside)), 1)
        self.assertEqual(gateway_run._active_schedule(experiment, schedule, outside), ())

    def test_budget_violation_never_retries_paid_block_automatically(self):
        limited = self.root / "limited.toml"
        limited.write_text(
            self.experiment.read_text().replace("max_output_tokens = 10", "max_output_tokens = 1")
        )
        limited_results = self.root / "limited.jsonl"
        with self._runtime():
            with self.assertRaisesRegex(gateway_run.GatewayRunError, "explicit rerun"):
                gateway_run.run_experiment(
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

    def test_max_calls_stops_upstream_and_counts_as_valid_unsolved(self):
        capped_results = self.root / "capped.jsonl"
        statuses = []
        self.upstream.gateway_status = 200

        def invoke_many(**kwargs):
            plan = json.loads(kwargs["plan_path"].read_text())
            url = urlsplit(kwargs["proxy_base_url"])
            body = json.dumps({
                "model": "client-controlled",
                "messages": [{"role": "user", "content": "private prompt"}],
                "stream": True,
            })
            for _ in range(4):
                conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
                conn.request(
                    "POST",
                    f"/cell/{kwargs['token']}/route/{plan['arm_digest']}",
                    body=body,
                    headers={"content-type": "application/json"},
                )
                response = conn.getresponse()
                statuses.append(response.status)
                response.read()
                conn.close()
            raise gateway_run.GatewayRunError(
                "adapter exited after budget rejection"
            )

        with self._runtime(), mock.patch.object(
            gateway_run, "_invoke_local", side_effect=invoke_many
        ):
            summary = gateway_run.run_experiment(
                self.experiment,
                results_path=capped_results,
                tasks_dir=self.tasks,
                exec_mode="local",
                environ={**os.environ, **self.env},
                adapters_dir=self.adapters,
            )
            upstream_after_first_run = len(self.upstream.requests)
            resumed = gateway_run.run_experiment(
                self.experiment,
                results_path=capped_results,
                tasks_dir=self.tasks,
                exec_mode="local",
                environ={**os.environ, **self.env},
                adapters_dir=self.adapters,
            )

        rows = results.read_jsonl_for_resume(capped_results).rows
        self.assertEqual((summary.blocks_completed, summary.rows_appended), (1, 2))
        self.assertEqual((resumed.blocks_skipped, resumed.rows_appended), (1, 0))
        self.assertEqual(len(self.upstream.requests), len(rows))
        self.assertEqual(len(self.upstream.requests), upstream_after_first_run)
        self.assertEqual(statuses.count(200), len(rows))
        self.assertEqual(statuses.count(429), 3 * len(rows))
        self.assertTrue(all(
            row["result"]["infrastructure_invalid_reason"] is None
            for row in rows
        ))
        self.assertTrue(all(
            row["result"]["budget_exhausted_reason"] == "max_calls"
            for row in rows
        ))
        self.assertTrue(all(not row["result"]["solved"] for row in rows))
        self.assertTrue(all(row["result"]["checker_score"] == 0.0 for row in rows))
        self.assertTrue(all(row["result"]["available"] for row in rows))
        self.assertTrue(all(not row["result"]["adapter_completed"] for row in rows))
        self.assertTrue(all(row["ledger_seal"]["record_count"] == 2 for row in rows))
        self.assertTrue(all(row["result"]["failure_class"] == "treatment" for row in rows))
        self.assertTrue(all(row["route_integrity"]["pass"] for row in rows))
        self.assertTrue(all(len(row["proxy_metrics"]["calls"]) == 1 for row in rows))
        self.assertTrue(all(
            row["proxy_metrics"]["calls"][0]["route"] is not None
            for row in rows
        ))
        report = gateway_report.aggregate(rows, bootstrap_replicates=20)
        self.assertEqual(report["blocks"], {
            "excluded": 0,
            "excluded_by_reason": {},
            "included": 1,
            "max_calls_affected": 1,
            "max_calls_rate": 1.0,
            "observed": 1,
        })
        self.assertTrue(all(
            arm["attempted_cells"] == 1
            and arm["metrics"]["solve_rate"]["estimate"] == 0.0
            for arm in report["arms"].values()
        ))
        ledgers = "".join(
            path.read_text()
            for path in (self.root / ".capped.gateway-ledgers").glob("*.jsonl")
        )
        self.assertEqual(ledgers.count('"error":"max_calls_exceeded"'), len(rows))
        self.assertNotIn(SECRET, capped_results.read_text() + ledgers)

    def test_invoke_local_stops_and_reaps_process_group_at_call_cap(self):
        root = self.root / "invoke-cap"
        root.mkdir()
        plan = root / "plan.json"
        instruction = root / "instruction.txt"
        workspace = root / "workspace"
        workspace.mkdir()
        plan.write_text("{}")
        instruction.write_text("test")
        child_pid = root / "child.pid"
        script = (
            "import os, pathlib, subprocess, time\n"
            f"p=subprocess.Popen(['sleep','60'])\n"
            f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        real_popen = subprocess.Popen
        with mock.patch.object(
            gateway_run.subprocess,
            "Popen",
            side_effect=lambda *args, **kwargs: real_popen(
                [sys.executable, "-c", script],
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                text=kwargs.get("text"),
                start_new_session=kwargs.get("start_new_session"),
            ),
        ):
            started = time.monotonic()
            result = gateway_run._invoke_local(
                plan_path=plan,
                instruction_path=instruction,
                workdir=workspace,
                proxy_base_url="http://127.0.0.1:1",
                token="cell",
                timeout_s=10,
                adapters_dir=self.adapters,
                max_calls_exceeded=lambda: child_pid.exists(),
            )
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(result["max_calls_exceeded"])
        pid = int(child_pid.read_text())
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("call-cap child process survived its process-group kill")

    def test_invoke_local_preserves_call_cap_when_entry_exits_immediately(self):
        root = self.root / "invoke-cap-exit"
        root.mkdir()
        plan = root / "plan.json"
        instruction = root / "instruction.txt"
        workspace = root / "workspace"
        workspace.mkdir()
        plan.write_text("{}")
        instruction.write_text("test")
        cap_signal = root / "cap.signal"
        script = (
            "import pathlib, sys\n"
            f"pathlib.Path({str(cap_signal)!r}).touch()\n"
            "sys.exit(1)\n"
        )
        real_popen = subprocess.Popen
        with mock.patch.object(
            gateway_run.subprocess,
            "Popen",
            side_effect=lambda *args, **kwargs: real_popen(
                [sys.executable, "-c", script],
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                text=kwargs.get("text"),
                start_new_session=kwargs.get("start_new_session"),
            ),
        ):
            result = gateway_run._invoke_local(
                plan_path=plan,
                instruction_path=instruction,
                workdir=workspace,
                proxy_base_url="http://127.0.0.1:1",
                token="cell",
                timeout_s=10,
                adapters_dir=self.adapters,
                max_calls_exceeded=cap_signal.exists,
            )
        self.assertTrue(result["max_calls_exceeded"])
        self.assertFalse(result["entry_timed_out"])

    def test_invoke_local_reaps_process_group_on_interrupt(self):
        root = self.root / "invoke-interrupt"
        root.mkdir()
        plan = root / "plan.json"
        instruction = root / "instruction.txt"
        workspace = root / "workspace"
        workspace.mkdir()
        plan.write_text("{}")
        instruction.write_text("test")
        child_pid = root / "child.pid"
        script = (
            "import pathlib, subprocess, time\n"
            f"p=subprocess.Popen(['sleep','60'])\n"
            f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        checks = 0

        def interrupt():
            nonlocal checks
            checks += 1
            if child_pid.exists() and checks > 1:
                raise KeyboardInterrupt
            return False

        real_popen = subprocess.Popen
        with mock.patch.object(
            gateway_run.subprocess,
            "Popen",
            side_effect=lambda *args, **kwargs: real_popen(
                [sys.executable, "-c", script],
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                text=kwargs.get("text"),
                start_new_session=kwargs.get("start_new_session"),
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                gateway_run._invoke_local(
                    plan_path=plan,
                    instruction_path=instruction,
                    workdir=workspace,
                    proxy_base_url="http://127.0.0.1:1",
                    token="cell",
                    timeout_s=10,
                    adapters_dir=self.adapters,
                    max_calls_exceeded=interrupt,
                )
        pid = int(child_pid.read_text())
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("interrupted child process survived its process-group kill")

    def test_proxy_evidence_honors_parser_integrity_verdict(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
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
        self.assertIn("malformed_sse_event", gateway_run._route_reasons(metrics, plan))

    def test_cold_prefix_evidence_fails_closed_on_missing_or_cached_usage(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = dataclasses.replace(
            next(item for item in plans if item.route_kind == "direct"),
            protocol="openai_responses",
            provider_prompt_mode="isolated_per_call_v1",
        )
        base = {
            "route": {
                "requested_model": plan.requested_model,
                "served_model": plan.requested_model,
                "provider": plan.requested_provider,
            },
            "stream": {"done": True},
            "route_evidence": {"pass": True, "reasons": []},
        }
        evidence = {
            "mode": "isolated_per_call_v1",
            "transform_id": gateway_run.gateway_spec.COLD_PREFIX_TRANSFORM_ID,
            "prefix_injected": True,
            "scope": "forwarded_request",
            "nonce_commitment": "a" * 64,
        }
        self.assertEqual(
            gateway_run._provider_cache_reasons(
                {"provider_cache": evidence}, base, plan
            ),
            ["cold_cache_usage_missing"],
        )
        cached = {
            **base,
            "usage": {"input_tokens_details": {"cached_tokens": 128}},
        }
        self.assertEqual(
            gateway_run._provider_cache_reasons(
                {"provider_cache": evidence}, cached, plan
            ),
            ["cold_cache_hit"],
        )
        uncached = {
            **base,
            "usage": {"input_tokens_details": {"cached_tokens": 0}},
        }
        self.assertEqual(
            gateway_run._provider_cache_reasons(
                {"provider_cache": evidence}, uncached, plan
            ),
            [],
        )

    def test_posthoc_caps_override_combined_max_calls_exhaustion(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = next(item for item in plans if item.route_kind == "direct")
        metrics = {
            "usage": {"input_tokens": 2, "output_tokens": 11},
            "route": {
                "requested_model": plan.requested_model,
                "metadata_requested_model": None,
                "served_model": plan.requested_model,
                "provider": plan.requested_provider,
                "attempts": [],
            },
            "stream": {"done": True},
            "route_evidence": {"pass": True, "reasons": []},
        }
        rejection = {"status": 429, "error": "max_calls_exceeded"}
        price = {
            plan.canonical_model: gateway_run.Price(
                gateway_run.Decimal("1"),
                gateway_run.Decimal("1"),
                "2026-07-22",
            )
        }

        cases = (
            (
                experiment.budget,
                metrics,
                "max_output_tokens_exceeded",
            ),
            (
                dataclasses.replace(
                    experiment.budget,
                    max_output_tokens=100,
                    usd_cap="0.000001",
                ),
                {
                    **metrics,
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
                "usd_cap_exceeded",
            ),
        )
        for budget, call_metrics, expected in cases:
            with self.subTest(expected=expected):
                calls, integrity, reason = gateway_run._proxy_evidence(
                    [{
                        "status": 200,
                        "gateway_metrics": call_metrics,
                        "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
                    }, rejection],
                    price,
                    budget,
                    plan,
                )
                self.assertEqual(reason, expected)
                self.assertTrue(integrity["pass"])
                self.assertEqual(len(calls), 1)

    def test_profile_specific_served_and_attempt_model_integrity(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        openrouter = next(item for item in plans if item.route_kind == "gateway")
        revision = "fake-model-2026-07-22"
        openrouter = dataclasses.replace(
            openrouter,
            allowed_models=(openrouter.requested_model, revision),
            model_match="exact_revision",
        )

        def metrics(*, served=revision, attempt=revision):
            return {
                "route": {
                    "requested_model": openrouter.requested_model,
                    "metadata_requested_model": openrouter.requested_model,
                    "served_model": served,
                    "provider": openrouter.requested_provider,
                    "attempts": [{
                        "provider": openrouter.requested_provider,
                        "model": attempt,
                        "status": 200,
                    }],
                },
                "stream": {"done": True},
                "route_evidence": {"pass": True, "reasons": []},
            }

        openrouter_reasons = gateway_run._route_reasons(metrics(), openrouter)
        self.assertIn("served_model_conflict", openrouter_reasons)
        self.assertIn("attempt_model_conflict", openrouter_reasons)

        vercel = dataclasses.replace(
            openrouter,
            gateway="vercel",
            model_match="model_family",
        )
        self.assertEqual(gateway_run._route_reasons(metrics(), vercel), [])
        undeclared = gateway_run._route_reasons(
            metrics(served="undeclared", attempt="undeclared"),
            vercel,
        )
        self.assertIn("served_model_conflict", undeclared)
        self.assertIn("attempt_model_conflict", undeclared)

        wrong_request = metrics()
        wrong_request["route"]["requested_model"] = revision
        wrong_request["route"]["metadata_requested_model"] = revision
        reasons = gateway_run._route_reasons(wrong_request, vercel)
        self.assertIn("requested_model_conflict", reasons)
        self.assertIn("metadata_requested_model_conflict", reasons)

        cloudflare = dataclasses.replace(
            openrouter,
            gateway="cloudflare",
            model_match="rolling_alias",
        )
        cloudflare_metrics = metrics(served="fake-model-2026-07-22")
        cloudflare_metrics["route"]["metadata_requested_model"] = None
        cloudflare_metrics["route"]["attempts"] = []
        self.assertEqual(
            gateway_run._route_reasons(cloudflare_metrics, cloudflare),
            [],
        )
        concentrate = dataclasses.replace(
            cloudflare,
            gateway="concentrate",
            requested_model="openai/fake-model",
            allowed_models=("openai/fake-model",),
        )
        concentrate_metrics = metrics(served="openai/fake-model")
        concentrate_metrics["route"]["requested_model"] = "openai/fake-model"
        concentrate_metrics["route"]["metadata_requested_model"] = None
        concentrate_metrics["route"]["served_model"] = "openai/fake-model"
        concentrate_metrics["route"]["attempts"] = []
        self.assertEqual(
            gateway_run._route_reasons(concentrate_metrics, concentrate),
            [],
        )

    def test_cache_activity_is_reported_without_invalidating_cell(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = plans[0]
        metrics = {
            "usage": {
                "input_tokens": 5,
                "input_tokens_details": {
                    "cached_tokens": 4,
                    "cache_write_tokens": 0,
                },
                "output_tokens": 2,
            },
            "route": {
                "requested_model": plan.requested_model,
                "metadata_requested_model": None,
                "served_model": plan.requested_model,
                "provider": plan.requested_provider,
                "attempts": [],
            },
            "stream": {"done": True},
            "route_evidence": {
                "pass": True,
                "reasons": [],
            },
        }
        calls, integrity, infrastructure = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "ts": "2026-07-22T12:00:00Z",
                "gateway_metrics": metrics,
                "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
            }],
            {plan.canonical_model: gateway_run.Price(
                gateway_run.Decimal("1"),
                gateway_run.Decimal("1"),
                "2026-07-22",
            )},
            experiment.budget,
            plan,
        )
        self.assertTrue(integrity["pass"])
        self.assertEqual(integrity["reasons"], [])
        self.assertIsNone(infrastructure)
        self.assertEqual(calls[0]["cache"], {
            "cached_input_tokens": 4,
            "cache_write_input_tokens": 0,
        })

        metrics["usage"]["input_tokens_details"] = {
            "cached_tokens": 0,
            "cache_write_tokens": 3,
        }
        calls, integrity, infrastructure = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "ts": "2026-07-22T12:00:00Z",
                "gateway_metrics": metrics,
                "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
            }],
            {plan.canonical_model: gateway_run.Price(
                gateway_run.Decimal("1"),
                gateway_run.Decimal("1"),
                "2026-07-22",
            )},
            experiment.budget,
            plan,
        )
        self.assertTrue(integrity["pass"])
        self.assertEqual(integrity["reasons"], [])
        self.assertIsNone(infrastructure)
        self.assertEqual(calls[0]["cache"], {
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 3,
        })

        metrics["usage"]["input_tokens_details"] = {
            "cached_tokens": 0,
            "cached_tokens_created": 7,
        }
        calls, integrity, infrastructure = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "ts": "2026-07-22T12:00:00Z",
                "gateway_metrics": metrics,
                "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
            }],
            {plan.canonical_model: gateway_run.Price(
                gateway_run.Decimal("1"),
                gateway_run.Decimal("1"),
                "2026-07-22",
            )},
            experiment.budget,
            plan,
        )
        self.assertTrue(integrity["pass"])
        self.assertEqual(integrity["reasons"], [])
        self.assertIsNone(infrastructure)
        self.assertEqual(calls[0]["cache"], {
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 7,
        })

    def test_vercel_reported_cost_is_timestamped_and_separate_from_frozen_price(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = dataclasses.replace(
            next(item for item in plans if item.route_kind == "gateway"),
            gateway="vercel",
        )
        metrics = {
            "route": {
                "requested_model": plan.requested_model,
                "metadata_requested_model": plan.requested_model,
                "served_model": plan.requested_model,
                "provider": plan.requested_provider,
                "attempts": [{
                    "provider": plan.requested_provider,
                    "model": plan.requested_model,
                    "status": 200,
                }],
                "gateway_metadata": {"cost": "0.00125"},
            },
            "route_evidence": {"pass": True, "reasons": []},
            "stream": {"done": True},
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        observed_at = "2026-07-22T12:34:56Z"
        calls, integrity, reason = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "ts": observed_at,
                "gateway_metrics": metrics,
                "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
            }],
            {
                plan.canonical_model: gateway_run.Price(
                    gateway_run.Decimal("1"),
                    gateway_run.Decimal("2"),
                    "2026-07-01T00:00:00Z",
                )
            },
            experiment.budget,
            plan,
        )

        self.assertTrue(integrity["pass"])
        self.assertIsNone(reason)
        self.assertEqual(calls[0]["costs"], {
            "gateway_reported": {
                "amount_usd": 0.00125,
                "currency": "USD",
                "effective_at": observed_at,
            },
            "frozen_list_estimate": {
                "amount_usd": 0.000011,
                "currency": "USD",
                "effective_at": "2026-07-01T00:00:00Z",
            },
        })
        self.assertEqual(
            gateway_report._costs(calls[0], 1, 1)["gateway_reported"],
            (0.00125, "USD", observed_at),
        )

    def test_errored_2xx_call_is_not_counted_as_unpriceable_success(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = next(item for item in plans if item.route_kind == "direct")
        calls, integrity, reason = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "error": "BrokenPipeError: client disconnected",
                "gateway_metrics": {
                    "route": {
                        "requested_model": plan.requested_model,
                        "served_model": plan.requested_model,
                        "provider": plan.requested_provider,
                    },
                    "usage": None,
                },
            }],
            {
                plan.canonical_model: gateway_run.Price(
                    gateway_run.Decimal("1"),
                    gateway_run.Decimal("2"),
                    "2026-07-01T00:00:00Z",
                )
            },
            experiment.budget,
            plan,
        )

        self.assertTrue(integrity["pass"])
        self.assertIsNone(reason)
        self.assertEqual(calls, [{
            "request_ordinal": None,
            "timing": None,
            "generation": None,
            "route": {
                "provider": plan.requested_provider,
                "served_model": plan.requested_model,
            },
            "tokens": None,
            "cache": None,
            "costs": None,
        }])

    def test_responses_terminal_outcomes_affect_availability_not_route_integrity(self):
        base = {
            "status": 200,
            "gateway_metrics": {
                "stream": {"done": True, "terminal_status": "completed"},
            },
        }
        self.assertTrue(gateway_run._upstream_row_available(base))

        incomplete = json.loads(json.dumps(base))
        incomplete["gateway_metrics"]["stream"]["terminal_status"] = "incomplete"
        self.assertFalse(gateway_run._upstream_row_available(incomplete))

        failed = json.loads(json.dumps(base))
        failed["gateway_metrics"]["stream"]["terminal_status"] = "failed"
        self.assertFalse(gateway_run._upstream_row_available(failed))

        cancelled = json.loads(json.dumps(base))
        cancelled["gateway_metrics"]["stream"]["terminal_status"] = "cancelled"
        self.assertFalse(gateway_run._upstream_row_available(cancelled))

    def test_failed_terminal_response_still_counts_billable_usage(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = next(item for item in plans if item.route_kind == "direct")
        metrics = {
            "stream": {"done": True, "terminal_status": "failed"},
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "route": {
                "requested_model": plan.requested_model,
                "metadata_requested_model": None,
                "served_model": plan.requested_model,
                "provider": plan.requested_provider,
                "attempts": [],
            },
            "route_evidence": {"pass": True, "reasons": []},
        }
        calls, integrity, reason = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "gateway_metrics": metrics,
                "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
            }],
            {
                plan.canonical_model: gateway_run.Price(
                    gateway_run.Decimal("1"),
                    gateway_run.Decimal("2"),
                    "2026-07-01T00:00:00Z",
                )
            },
            experiment.budget,
            plan,
        )

        self.assertTrue(integrity["pass"])
        self.assertIsNone(reason)
        self.assertEqual(
            calls[0]["costs"]["frozen_list_estimate"]["amount_usd"],
            0.000011,
        )

    def test_openrouter_streamed_usage_cost_flows_into_call_costs(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = next(item for item in plans if item.route_kind == "gateway")
        final = {
            "model": plan.requested_model,
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "cost": 0.00125,
            },
            "openrouter_metadata": {
                "requested": plan.requested_model,
                "endpoints": {"available": [{
                    "provider": plan.requested_provider,
                    "selected": True,
                }]},
            },
        }
        payload = (
            f"data: {json.dumps(final, separators=(',', ':'))}\n\n"
            "data: [DONE]\n\n"
        ).encode()
        metrics = gateway_metrics.parse_chat_sse(
            [(11.0, payload)],
            requested_model=plan.requested_model,
            requested_provider=plan.requested_provider,
            allowed_models=plan.allowed_models,
            allowed_providers=plan.allowed_providers,
            gateway="openrouter",
            started_at=10.0,
            completed_at=12.0,
        )
        observed_at = "2026-07-22T12:34:56Z"
        calls, integrity, reason = gateway_run._proxy_evidence(
            [{
                "status": 200,
                "ts": observed_at,
                "gateway_metrics": metrics,
                "provider_cache": PROVIDER_DEFAULT_EVIDENCE,
            }],
            {
                plan.canonical_model: gateway_run.Price(
                    gateway_run.Decimal("1"),
                    gateway_run.Decimal("2"),
                    "2026-07-01T00:00:00Z",
                )
            },
            experiment.budget,
            plan,
        )

        self.assertTrue(integrity["pass"])
        self.assertIsNone(reason)
        self.assertEqual(calls[0]["route"]["gateway_metadata"], {"cost": 0.00125})
        self.assertEqual(calls[0]["costs"]["gateway_reported"], {
            "amount_usd": 0.00125,
            "currency": "USD",
            "effective_at": observed_at,
        })

    def test_vercel_malformed_or_unstamped_cost_remains_absent(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = dataclasses.replace(
            next(item for item in plans if item.route_kind == "gateway"),
            gateway="vercel",
        )
        prices = {
            plan.canonical_model: gateway_run.Price(
                gateway_run.Decimal("1"),
                gateway_run.Decimal("2"),
                "2026-07-01T00:00:00Z",
            )
        }
        invalid = (
            None,
            "",
            " 0.1",
            "-0.1",
            "01.2",
            "1_000",
            "NaN",
            "Infinity",
            "1e9999",
            -1,
            float("inf"),
            True,
        )
        for cost in invalid:
            with self.subTest(cost=cost):
                metrics = {
                    "route": {
                        "gateway_metadata": (
                            {} if cost is None else {"cost": cost}
                        ),
                    },
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }
                costs, amount = gateway_run._price_call(
                    metrics,
                    prices,
                    plan,
                    "2026-07-22T12:34:56Z",
                )
                self.assertNotIn("gateway_reported", costs)
                self.assertIn("frozen_list_estimate", costs)
                self.assertEqual(amount, gateway_run.Decimal("0.000011"))

        costs, _amount = gateway_run._price_call(
            {
                "route": {"gateway_metadata": {"cost": "0.00125"}},
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
            prices,
            plan,
            "not-a-timestamp",
        )
        self.assertNotIn("gateway_reported", costs)
        self.assertIn("frozen_list_estimate", costs)

    def test_price_coverage_and_auth_failures_fail_closed(self):
        experiment = gateway_run.gateway_spec.load_experiment(self.experiment)
        with mock.patch.object(gateway_run.pi, "version", return_value="fake-pi 1.0"):
            report = gateway_run.doctor_experiment(
                self.experiment,
                tasks_dir=self.tasks,
                environ={
                    **self.env,
                    gateway_run.FROZEN_PRICES_ENV: json.dumps({
                        "other/model": {
                            "input_per_million": "1",
                            "output_per_million": "1",
                            "effective_at": "2026-07-22",
                        }
                    }),
                },
            )
        self.assertFalse(report["usd_cap_enforceable"])
        self.assertEqual(report["missing_price_models"], ["openai/fake-model"])
        incomplete_env = {
            **self.env,
            gateway_run.FROZEN_PRICES_ENV: json.dumps({
                "other/model": {
                    "input_per_million": "1",
                    "output_per_million": "1",
                    "effective_at": "2026-07-22",
                }
            }),
        }
        with mock.patch.object(gateway_run.pi, "version", return_value="fake-pi 1.0"):
            with self.assertRaisesRegex(gateway_run.GatewayRunError, "missing"):
                gateway_run.run_experiment(
                    self.experiment,
                    results_path=self.results,
                    tasks_dir=self.tasks,
                    exec_mode="local",
                    environ=incomplete_env,
                    adapters_dir=self.adapters,
                )
        self.assertFalse(self.results.exists())

        plans, _secrets = gateway_run.gateway_spec.compile_route_plans(
            experiment,
            environ=self.env,
            admitted_auth_envs={"DIRECT_KEY", "GATEWAY_KEY"},
        )
        plan = plans[0]
        _calls, integrity, reason = gateway_run._proxy_evidence(
            [{"status": 401}],
            {plan.canonical_model: gateway_run.Price(
                gateway_run.Decimal("1"), gateway_run.Decimal("1"), "2026-07-22"
            )},
            experiment.budget,
            plan,
        )
        self.assertTrue(integrity["pass"])
        self.assertEqual(reason, "upstream_auth_failure")

        for status in (429, 503):
            with self.subTest(status=status):
                _calls, integrity, reason = gateway_run._proxy_evidence(
                    [{"status": status}],
                    {plan.canonical_model: gateway_run.Price(
                        gateway_run.Decimal("1"),
                        gateway_run.Decimal("1"),
                        "2026-07-22",
                    )},
                    experiment.budget,
                    plan,
                )
                self.assertTrue(integrity["pass"])
                self.assertIsNone(reason)

    def test_docker_fails_closed_in_local_mvp(self):
        with self._runtime():
            with self.assertRaisesRegex(gateway_run.GatewayRunError, "unsupported"):
                gateway_run.run_experiment(
                    self.experiment,
                    results_path=self.results,
                    tasks_dir=self.tasks,
                    exec_mode="docker",
                    environ={**os.environ, **self.env},
                    adapters_dir=self.adapters,
                )


class GatewayVerifierCommitTests(unittest.TestCase):
    def test_historical_verification_can_skip_only_tree_equality(self):
        commit = "a" * 40
        with (
            mock.patch.object(
                gateway_probe_publish,
                "_detect_verifier_commit",
                return_value=commit,
            ),
            mock.patch.object(
                gateway_probe_publish,
                "_assert_verifier_tree_matches",
            ) as assert_tree,
        ):
            self.assertEqual(
                gateway_probe_publish._verified_with_commit(None),
                commit,
            )
            assert_tree.assert_called_once_with(commit)
            assert_tree.reset_mock()
            self.assertEqual(
                gateway_probe_publish._verified_with_commit(
                    None,
                    require_tree_match=False,
                ),
                commit,
            )
            assert_tree.assert_not_called()


if __name__ == "__main__":
    unittest.main()
