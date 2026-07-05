#!/usr/bin/env python3
"""Tests for the container execution backend that need no Docker daemon.

Covers the daemon-independent plumbing of ``--exec docker``:
  - ``docker_exec.build_docker_cmd`` assembles the expected ``docker run`` argv
    (mounts, workdir, auth, trailing entrypoint command),
  - ``docker_exec._parse_result`` extracts the sentinel-tagged result dict,
  - ``docker_exec.preflight`` raises ``DockerUnavailable`` when the daemon or
    image is missing (the signal the runner uses to fall back),
  - ``run.invoke_adapter`` falls back to local execution on ``DockerUnavailable``,
  - ``entry.py`` runs a real adapter against a mounted workdir and emits a
    parseable result line (proves the in-container round-trip on the host).

The live container round-trip itself is proven separately (see the worker
report) since it requires a running daemon and a built image.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(BENCH_DIR, "tests", "fixtures")
ENTRY_PY = os.path.join(BENCH_DIR, "entry.py")

sys.path.insert(0, BENCH_DIR)

import docker_exec  # noqa: E402
import run  # noqa: E402


class TestBuildDockerCmd(unittest.TestCase):
    def test_cmd_shape_and_mounts(self):
        cmd = docker_exec.build_docker_cmd(
            harness="codex",
            workdir="/tmp/wd",
            model="gpt-5.5-medium",
            timeout_s=240,
            adapters_dir="/repo/bench/adapters",
            image="openbench-harness:latest",
            instruction_path="/tmp/instr.txt",
        )
        # docker run --rm ... <image> python3 /bench/entry.py codex gpt-5.5-medium 240
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertEqual(cmd[-6], "openbench-harness:latest")
        self.assertEqual(cmd[-5:],
                         ["python3", "/bench/entry.py",
                          "codex", "gpt-5.5-medium", "240"])
        joined = " ".join(cmd)
        self.assertIn("/tmp/wd:/work", joined)
        self.assertIn("/repo/bench/adapters:/bench/adapters:ro", joined)
        self.assertIn("/tmp/instr.txt:/bench/instruction.txt:ro", joined)
        self.assertIn("entry.py:/bench/entry.py:ro", joined)
        self.assertIn("HOME=/root", joined)

    def test_api_key_passthrough_by_name_only(self):
        # A set key is forwarded as a bare `-e VAR` (docker reads the value
        # from the client env; the secret never lands in argv); unset keys are
        # not mentioned at all.
        orig_env = dict(os.environ)
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        os.environ.pop("MOONSHOT_API_KEY", None)
        try:
            cmd = docker_exec.build_docker_cmd(
                harness="pi", workdir="/tmp/wd", model="deepseek-v4-flash",
                timeout_s=240, adapters_dir="/repo/bench/adapters",
                image="openbench-harness:latest",
                instruction_path="/tmp/instr.txt",
            )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)
        self.assertIn("DEEPSEEK_API_KEY", cmd)
        self.assertNotIn("MOONSHOT_API_KEY", cmd)
        self.assertFalse(any("sk-test" in a for a in cmd),
                         "secret value must not appear in argv")

    def test_auth_mount_readonly_when_present(self):
        # Use a fake HOME so the test is deterministic regardless of the host.
        home = tempfile.mkdtemp(prefix="fake_home_")
        try:
            os.makedirs(os.path.join(home, ".codex"))
            orig = os.path.expanduser
            os.path.expanduser = lambda p: home if p == "~" else orig(p)
            try:
                args = docker_exec._auth_mount_args("codex")
            finally:
                os.path.expanduser = orig
            self.assertIn("-v", args)
            self.assertTrue(any(a.endswith(".codex:/bench/auth/.codex:ro")
                                for a in args),
                            f"expected read-only staged .codex mount, got {args}")
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)


class TestParseResult(unittest.TestCase):
    def test_extracts_last_sentinel_line(self):
        stdout = (
            "noise from the CLI\n"
            + docker_exec.RESULT_SENTINEL + " "
            + json.dumps({"completed": True, "tokens": 7}) + "\n"
        )
        result = docker_exec._parse_result(stdout)
        self.assertEqual(result, {"completed": True, "tokens": 7})

    def test_missing_sentinel_returns_none(self):
        self.assertIsNone(docker_exec._parse_result("just output\n"))


class TestPreflight(unittest.TestCase):
    def test_raises_when_daemon_down(self):
        orig = docker_exec.daemon_running
        docker_exec.daemon_running = lambda: False
        try:
            with self.assertRaises(docker_exec.DockerUnavailable):
                docker_exec.preflight("openbench-harness:latest")
        finally:
            docker_exec.daemon_running = orig

    def test_raises_when_image_missing(self):
        od, oi = docker_exec.daemon_running, docker_exec.image_exists
        docker_exec.daemon_running = lambda: True
        docker_exec.image_exists = lambda image: False
        try:
            with self.assertRaises(docker_exec.DockerUnavailable):
                docker_exec.preflight("nope:latest")
        finally:
            docker_exec.daemon_running, docker_exec.image_exists = od, oi


class TestInvokeAdapterFallback(unittest.TestCase):
    def test_docker_unavailable_falls_back_to_local(self):
        # Force the docker backend to report unavailable; invoke_adapter should
        # transparently run the fixture adapter locally and report exec="local".
        orig = docker_exec.run_in_container

        def boom(*a, **k):
            raise docker_exec.DockerUnavailable("forced")

        docker_exec.run_in_container = boom
        workdir = tempfile.mkdtemp(prefix="fallback_wd_")
        try:
            result, exec_used = run.invoke_adapter(
                "docker", "fake_adapter", "solve it", workdir,
                "gpt-5.5-medium", 30, FIXTURES_DIR,
                "openbench-harness:latest", docker_fallback=True,
            )
            self.assertEqual(exec_used, "local")
            self.assertTrue(result["completed"])
            self.assertTrue(os.path.exists(os.path.join(workdir, "done.txt")))
        finally:
            docker_exec.run_in_container = orig
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    def test_no_fallback_reraises(self):
        orig = docker_exec.run_in_container

        def boom(*a, **k):
            raise docker_exec.DockerUnavailable("forced")

        docker_exec.run_in_container = boom
        try:
            with self.assertRaises(docker_exec.DockerUnavailable):
                run.invoke_adapter(
                    "docker", "fake_adapter", "x", tempfile.mkdtemp(),
                    "gpt-5.5-medium", 30, FIXTURES_DIR,
                    "img:latest", docker_fallback=False,
                )
        finally:
            docker_exec.run_in_container = orig


class TestEntryRoundTrip(unittest.TestCase):
    """Exercise entry.py on the host with BENCH_* path overrides."""

    def _run_entry(self, harness, workdir, instruction="do it", timeout=30):
        instr_fd, instr_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(instr_fd, "w") as fh:
            fh.write(instruction)
        env = dict(os.environ)
        env["BENCH_ADAPTERS_DIR"] = FIXTURES_DIR
        env["BENCH_INSTRUCTION_PATH"] = instr_path
        env["BENCH_WORKDIR"] = workdir
        try:
            proc = subprocess.run(
                [sys.executable, ENTRY_PY, harness, "gpt-5.5-medium", str(timeout)],
                capture_output=True, text=True, env=env, timeout=30,
            )
        finally:
            os.unlink(instr_path)
        # Extract the sentinel line.
        result = None
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith(docker_exec.RESULT_SENTINEL):
                result = json.loads(line[len(docker_exec.RESULT_SENTINEL):].strip())
                break
        return proc, result

    def test_null_round_trip(self):
        workdir = tempfile.mkdtemp(prefix="entry_null_")
        try:
            proc, result = self._run_entry("null", workdir)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIsNotNone(result, "entry.py must emit a result sentinel")
            self.assertTrue(result["completed"])
            self.assertEqual(result["cmd"], "null")
        finally:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    def test_real_adapter_round_trip_writes_workdir(self):
        # entry.py must run the real adapter against the mounted workdir.
        workdir = tempfile.mkdtemp(prefix="entry_fake_")
        try:
            proc, result = self._run_entry("fake_adapter", workdir)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIsNotNone(result)
            self.assertTrue(result["completed"])
            self.assertEqual(result["tokens"], 42)
            self.assertTrue(
                os.path.exists(os.path.join(workdir, "done.txt")),
                "adapter run via entry.py must edit the mounted workdir")
        finally:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
