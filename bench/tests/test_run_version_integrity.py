#!/usr/bin/env python3
"""Tests for Docker/local harness version provenance in bench/run.py."""

import json
import os
import sys
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import docker_exec  # noqa: E402
import run  # noqa: E402


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
