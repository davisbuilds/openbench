#!/usr/bin/env python3
"""Tests for Docker/local harness version provenance in bench/run.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


from obench import docker_exec  # noqa: E402
from obench import run  # noqa: E402


class TestContainerVersionFileParsing(unittest.TestCase):
    def test_parse_container_cli_versions_accepts_string_object(self):
        payload = json.dumps({
            "codex": "codex 0.144.1",
            "pi": "0.80.6",
            "cursor": "2026.07.09-a3815c0",
            "ignored": 123,
        })
        self.assertEqual(run.parse_container_cli_versions(payload), {
            "codex": "codex 0.144.1",
            "pi": "0.80.6",
            "cursor": "2026.07.09-a3815c0",
        })

    def test_parse_container_cli_versions_rejects_malformed_or_non_object(self):
        self.assertEqual(run.parse_container_cli_versions("not json"), {})
        self.assertEqual(run.parse_container_cli_versions("[]"), {})


class TestVersionPreflight(unittest.TestCase):
    def _dockerfile(self, version="0.144.1"):
        fh = tempfile.NamedTemporaryFile("w", delete=False)
        fh.write(f"ARG CODEX_VERSION={version}\n")
        fh.close()
        self.addCleanup(lambda: os.path.exists(fh.name) and os.unlink(fh.name))
        return fh.name

    def test_manifest_proxy_metadata_does_not_select_a_cli_pin(self):
        manifest = SimpleNamespace(base_adapter=None, proxy_adapter="codex", kind="manifest")
        self.assertIsNone(run._pin_key_for_harness("custom", manifest))

    def test_config_variant_uses_its_executed_base_adapter_pin(self):
        variant = SimpleNamespace(base_adapter="codex", proxy_adapter="codex",
                                  kind="config-variant")
        self.assertEqual(run._pin_key_for_harness("custom", variant), "codex")

    def test_matching_mocked_cli_version_passes(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.144.1\n", stderr="")

        drift = run.host_version_drift(
            ["codex"], dockerfile=self._dockerfile(), subprocess_runner=fake_run)
        self.assertEqual(drift, [])
        self.assertEqual(calls[0][0], ["codex", "--version"])

    def _run_main(self, drift, extra_args=None, image_result=None):
        emitted = []

        def fake_run_cell(*args, **kwargs):
            emitted.append(kwargs["version_drift"])
            if image_result == ([], False):
                self.assertEqual(kwargs["exec_mode"], "local")
            return {
                "success": False, "score": 0.0, "completed": True,
                "checker_exit": 1, "exec_mode": "local",
            }

        argv = ["--harness", "codex", "--task", "fake-task",
                "--results-path", os.path.join(tempfile.gettempdir(), "version-gate.jsonl")]
        argv.extend(extra_args or [])
        patches = [
            mock.patch.object(run, "host_version_drift", return_value=drift),
            mock.patch.object(run, "probe_version", return_value="codex-cli 0.144.0"),
            mock.patch.object(run, "load_existing_run_ids", return_value=set()),
            mock.patch.object(run, "run_cell", side_effect=fake_run_cell),
            mock.patch.object(run, "append_row"),
        ]
        if image_result is not None:
            patches.append(mock.patch.object(
                run, "image_version_drift", return_value=image_result))
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            run_cell_mock = mocks[3]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = run.main(argv)
        return code, emitted, run_cell_mock.call_count, stderr.getvalue()

    def test_matching_image_labels_pass(self):
        labels = json.dumps({"org.openbench.cli.codex": "0.144.1"})
        proc = SimpleNamespace(returncode=0, stdout=labels, stderr="")
        drift, available = run.image_version_drift(
            "image:tag", ["codex"], dockerfile=self._dockerfile(),
            subprocess_runner=lambda *args, **kwargs: proc)
        self.assertTrue(available)
        self.assertEqual(drift, [])

    def test_image_mismatch_refuses_before_any_cell(self):
        drift = [{"harness": "codex", "actual": "0.144.0",
                  "expected": "0.144.1", "cli": "codex"}]
        code, emitted, call_count, stderr = self._run_main(
            [], ["--exec", "docker"], (drift, True))
        self.assertEqual((code, emitted, call_count), (2, [], 0))
        self.assertIn("codex: image=0.144.0 pin=0.144.1", stderr)
        self.assertIn("docker build -t openbench-harness:latest obench/docker", stderr)

    def test_image_mismatch_with_flag_marks_every_row(self):
        drift = [{"harness": "codex", "actual": "0.144.0",
                  "expected": "0.144.1", "cli": "codex"}]
        code, emitted, call_count, stderr = self._run_main(
            [], ["--exec", "docker", "--allow-version-drift"], (drift, True))
        self.assertEqual((code, emitted, call_count), (0, [True], 1))
        self.assertIn("version drift allowed", stderr)

    def test_missing_image_defers_to_existing_fallback_with_build_hint(self):
        code, emitted, call_count, stderr = self._run_main(
            [], ["--exec", "docker"], ([], False))
        self.assertEqual((code, emitted, call_count), (0, [False], 1))
        self.assertIn("falling back to the validated host lane", stderr)
        self.assertIn("docker build -t openbench-harness:latest obench/docker", stderr)

    def test_missing_image_without_fallback_refuses_with_build_hint(self):
        code, emitted, call_count, stderr = self._run_main(
            [], ["--exec", "docker", "--no-docker-fallback"], ([], False))
        self.assertEqual((code, emitted, call_count), (2, [], 0))
        self.assertIn("Version preflight failed: cannot inspect Docker image", stderr)
        self.assertIn("docker build -t openbench-harness:latest obench/docker", stderr)

    def test_mismatch_refuses_before_any_cell(self):
        drift = [{"harness": "codex", "actual": "0.144.0", "expected": "0.144.1",
                  "cli": "codex"}]
        code, emitted, call_count, stderr = self._run_main(drift)
        self.assertEqual(code, 2)
        self.assertEqual(emitted, [])
        self.assertEqual(call_count, 0)
        self.assertIn("host=0.144.0 pin=0.144.1", stderr)
        self.assertIn("python3 -m obench.bump_clis --sync-host", stderr)

    def test_mismatch_with_flag_marks_every_row(self):
        drift = [{"harness": "codex", "actual": "0.144.0", "expected": "0.144.1",
                  "cli": "codex"}]
        code, emitted, call_count, _stderr = self._run_main(
            drift, ["--allow-version-drift"])
        self.assertEqual(code, 0)
        self.assertEqual(call_count, 1)
        self.assertEqual(emitted, [True])


class TestHarnessVersionSource(unittest.TestCase):
    def test_docker_fallback_exception_keeps_local_exec_provenance(self):
        adapters_dir = tempfile.mkdtemp(prefix="bad_adapter_")
        with open(os.path.join(adapters_dir, "bad.py"), "w", encoding="utf-8") as fh:
            fh.write("def run(*args):\n    raise RuntimeError('local boom')\n")
        workdir = tempfile.mkdtemp(prefix="bad_workdir_")
        orig = docker_exec.run_in_container
        docker_exec.run_in_container = lambda *a, **k: (_ for _ in ()).throw(
            docker_exec.DockerUnavailable("forced"))
        try:
            with self.assertRaises(RuntimeError) as cm:
                run.invoke_adapter(
                    "docker", "bad", "do it", workdir,
                    "gpt-5.5-medium", 30, adapters_dir,
                    "openbench-harness:latest", docker_fallback=True,
                )
        finally:
            docker_exec.run_in_container = orig
            import shutil
            shutil.rmtree(adapters_dir, ignore_errors=True)
            shutil.rmtree(workdir, ignore_errors=True)
        self.assertEqual(getattr(cm.exception, "bench_exec_used"), "local")

    def test_local_uses_host_probe_value(self):
        version, source = run.harness_version_for_source(
            "pi", "local", "host-pi-1", "image:tag", "sha256:image",
            container_versions_reader=lambda image, digest: {"pi": "container-pi-2"},
        )
        self.assertEqual((version, source), ("host-pi-1", "host"))

    def test_docker_uses_container_version_value(self):
        calls = []

        def reader(image, digest):
            calls.append((image, digest))
            return {"pi": "container-pi-2"}

        version, source = run.harness_version_for_source(
            "pi", "docker", "host-pi-1", "image:tag", "sha256:image", reader,
        )
        self.assertEqual((version, source), ("container-pi-2", "container"))
        self.assertEqual(calls, [("image:tag", "sha256:image")])

    def test_null_reports_builtin_but_marks_docker_source(self):
        version, source = run.harness_version_for_source(
            "null", "docker", "builtin", "image:tag", "sha256:image",
            container_versions_reader=lambda image, digest: {},
        )
        self.assertEqual((version, source), ("builtin", "container"))


if __name__ == "__main__":
    unittest.main()
