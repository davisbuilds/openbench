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
            # codex mounts individual auth files, never the whole ~/.codex
            # (which also holds multi-GB worktrees/sessions).
            os.makedirs(os.path.join(home, ".codex"))
            with open(os.path.join(home, ".codex", "auth.json"), "w") as fh:
                fh.write("{}")
            orig = os.path.expanduser
            os.path.expanduser = lambda p: home if p == "~" else orig(p)
            try:
                args = docker_exec._auth_mount_args("codex")
            finally:
                os.path.expanduser = orig
            self.assertIn("-v", args)
            self.assertTrue(
                any(a.endswith(".codex/auth.json:/bench/auth/.codex/auth.json:ro")
                    for a in args),
                f"expected read-only staged auth.json mount, got {args}")
            self.assertFalse(
                any(a.endswith(".codex:/bench/auth/.codex:ro") for a in args),
                "must not mount the whole ~/.codex dir")
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
                docker_exec.preflight("openbench-harness:latest",
                                      retries=2, delay_s=0)
        finally:
            docker_exec.daemon_running = orig

    def test_raises_when_image_missing(self):
        od, oi, ort = (docker_exec.daemon_running, docker_exec.image_exists,
                       docker_exec._retag_corrupt_image)
        docker_exec.daemon_running = lambda: True
        docker_exec.image_exists = lambda image: False
        docker_exec._retag_corrupt_image = lambda image: False
        try:
            with self.assertRaises(docker_exec.DockerUnavailable):
                docker_exec.preflight("nope:latest", retries=2, delay_s=0)
        finally:
            (docker_exec.daemon_running, docker_exec.image_exists,
             docker_exec._retag_corrupt_image) = od, oi, ort

    def test_retags_corrupt_image_when_images_lists_id(self):
        calls = []
        state = {"tagged": False}

        class FakeProc:
            def __init__(self, stdout="", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, "", returncode

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "images"]:
                return FakeProc("openbench-harness\tlatest\tsha256:abc123\n")
            if cmd[:2] == ["docker", "tag"]:
                state["tagged"] = True
                return FakeProc()
            raise AssertionError(f"unexpected docker command: {cmd}")

        od, oi, osr = (docker_exec.daemon_running, docker_exec.image_exists,
                       docker_exec.subprocess.run)
        docker_exec.daemon_running = lambda: True
        docker_exec.image_exists = lambda image: state["tagged"]
        docker_exec.subprocess.run = fake_run
        try:
            docker_exec.preflight("openbench-harness:latest", retries=1, delay_s=0)
        finally:
            (docker_exec.daemon_running, docker_exec.image_exists,
             docker_exec.subprocess.run) = od, oi, osr
        self.assertIn(["docker", "tag", "sha256:abc123", "openbench-harness:latest"], calls)

    def test_retag_ignores_non_matching_repo_tag(self):
        class FakeProc:
            stdout = "other\tlatest\tsha256:nope\n"
            stderr = ""
            returncode = 0

        orig = docker_exec.subprocess.run
        docker_exec.subprocess.run = lambda *a, **k: FakeProc()
        try:
            self.assertFalse(docker_exec._retag_corrupt_image("openbench-harness:latest"))
        finally:
            docker_exec.subprocess.run = orig


class TestContainerCleanup(unittest.TestCase):
    """run_in_container must remove the container by name on EVERY exit path.

    A wedged inner CLI holds the container's stdout pipe open past the adapter
    timeout, so killing the `docker run` client does not stop the container;
    only removal by name does. These tests fake subprocess.run to record which
    docker commands fire on each path.
    """

    def _patch(self, main_run_effect):
        """Replace docker_exec's subprocess.run; return the recorded call log.

        ``main_run_effect(cmd)`` handles the `docker run` invocation (return a
        fake proc or raise). `docker rm -f` / `docker ps` get gone-container
        stubs so _force_remove_container verifies removal on the first try.
        """
        calls = []

        class FakeProc:
            def __init__(self, stdout="", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, "", returncode

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "run"]:
                return main_run_effect(cmd)
            return FakeProc()  # rm -f / ps probe: container gone

        self._orig = (docker_exec.subprocess.run, docker_exec.preflight)
        docker_exec.subprocess.run = fake_run
        docker_exec.preflight = lambda image: None
        self.addCleanup(self._unpatch)
        return calls, FakeProc

    def _unpatch(self):
        docker_exec.subprocess.run, docker_exec.preflight = self._orig

    def _invoke(self):
        return docker_exec.run_in_container(
            "pi", "do it", "/tmp/wd", "deepseek-v4-flash", 900,
            "/repo/bench/adapters")

    def _rm_calls(self, calls):
        return [c for c in calls if c[:3] == ["docker", "rm", "-f"]]

    def test_timeout_path_removes_container(self):
        def effect(cmd):
            raise subprocess.TimeoutExpired(cmd, 960, output=b"partial", stderr=None)
        calls, _ = self._patch(effect)
        result = self._invoke()
        self.assertFalse(result["completed"])
        self.assertIn("timeout", result["error"])
        self.assertEqual(result["output_tail"], "partial")
        self.assertEqual(len(self._rm_calls(calls)), 1)

    def test_unexpected_exception_still_removes_container(self):
        def effect(cmd):
            raise RuntimeError("daemon hiccup")
        calls, _ = self._patch(effect)
        with self.assertRaises(RuntimeError):
            self._invoke()
        self.assertEqual(len(self._rm_calls(calls)), 1)

    def test_clean_return_also_sweeps_name(self):
        # Even when `docker run --rm` exits normally, removal-by-name fires as
        # a cheap no-op so a glitched --rm can't leave a wedged container.
        sentinel = docker_exec.RESULT_SENTINEL + ' {"completed": true}\n'
        fake_holder = []

        def effect(cmd):
            return fake_holder[0](stdout=sentinel)

        calls, FakeProc = self._patch(effect)
        fake_holder.append(FakeProc)
        result = self._invoke()
        self.assertTrue(result["completed"])
        self.assertEqual(len(self._rm_calls(calls)), 1)

    def test_force_remove_retries_until_gone(self):
        # First rm leaves the container visible in `docker ps` (wedged CLI on a
        # busy daemon); the retry succeeds.
        calls = []

        class FakeProc:
            def __init__(self, stdout=""):
                self.stdout, self.stderr, self.returncode = stdout, "", 0

        state = {"alive": True}

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "ps"]:
                if state["alive"]:
                    state["alive"] = False
                    return FakeProc(stdout="abc123\n")  # still there
                return FakeProc()
            return FakeProc()

        orig = docker_exec.subprocess.run
        docker_exec.subprocess.run = fake_run
        try:
            ok = docker_exec._force_remove_container("openbench_x", delay_s=0)
        finally:
            docker_exec.subprocess.run = orig
        self.assertTrue(ok)
        self.assertEqual(
            len([c for c in calls if c[:3] == ["docker", "rm", "-f"]]), 2)


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
