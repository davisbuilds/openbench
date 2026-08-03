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

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

FIXTURES_DIR = os.path.join(BENCH_DIR, "tests", "fixtures")
ENTRY_PY = os.path.join(BENCH_DIR, "entry.py")
from obench import docker_exec  # noqa: E402
from obench import run  # noqa: E402


class TestBuildDockerCmd(unittest.TestCase):
    def test_cmd_shape_and_mounts(self):
        cmd = docker_exec.build_docker_cmd(
            harness="codex",
            workdir="/tmp/wd",
            model="gpt-5.5-medium",
            timeout_s=240,
            adapters_dir="/repo/obench/adapters",
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
        self.assertIn("/repo/obench/adapters:/bench/adapters:ro", joined)
        self.assertIn("/tmp/instr.txt:/bench/instruction.txt:ro", joined)
        self.assertIn("entry.py:/bench/entry.py:ro", joined)
        self.assertIn("HOME=/root", joined)

    def test_task_container_workdir_mount(self):
        cmd = docker_exec.build_docker_cmd(
            harness="null", workdir="/tmp/wd", model="none", timeout_s=10,
            adapters_dir="/repo/obench/adapters", image="task:pinned",
            instruction_path="/tmp/instr", container_workdir="/app")
        joined = " ".join(cmd)
        self.assertIn("/tmp/wd:/app", joined)
        self.assertIn("-w /app", joined)

    def test_task_docker_spec(self):
        with tempfile.TemporaryDirectory() as task:
            with open(os.path.join(task, "task.toml"), "w", encoding="utf-8") as fh:
                fh.write('docker_image = "task:pinned"\ndocker_workdir = "/app"\n')
            self.assertEqual(run.task_docker_spec(task), ("task:pinned", "/app"))

    def test_api_key_passthrough_by_name_only(self):
        # A set key is forwarded as a bare `-e VAR` (docker reads the value
        # from the client env; the secret never lands in argv); unset keys are
        # not mentioned at all. Pass-through is scoped to the exact cell so
        # unrelated agent containers cannot read extra credentials.
        orig_env = dict(os.environ)
        # deepseek-v4-flash routes through OpenRouter, so ITS key is the one
        # that must be forwarded; the vendor key must not be.
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ["CURSOR_API_KEY"] = "cursor-test"
        os.environ.pop("MOONSHOT_API_KEY", None)
        try:
            cmd = docker_exec.build_docker_cmd(
                harness="pi", workdir="/tmp/wd", model="deepseek-v4-flash",
                timeout_s=240, adapters_dir="/repo/obench/adapters",
                image="openbench-harness:latest",
                instruction_path="/tmp/instr.txt",
            )
            claude_cmd = docker_exec.build_docker_cmd(
                harness="claude", workdir="/tmp/wd", model="claude-opus-4-8",
                timeout_s=240, adapters_dir="/repo/obench/adapters",
                image="openbench-harness:latest",
                instruction_path="/tmp/instr.txt",
            )
            cursor_cmd = docker_exec.build_docker_cmd(
                harness="cursor", workdir="/tmp/wd", model="claude-opus-4-8",
                timeout_s=240, adapters_dir="/repo/obench/adapters",
                image="openbench-harness:latest",
                instruction_path="/tmp/instr.txt",
            )
            codex_opus_cmd = docker_exec.build_docker_cmd(
                harness="codex", workdir="/tmp/wd", model="claude-opus-4-8",
                timeout_s=240, adapters_dir="/repo/obench/adapters",
                image="openbench-harness:latest",
                instruction_path="/tmp/instr.txt",
            )
            grokbuild_cmd = docker_exec.build_docker_cmd(
                harness="grokbuild", workdir="/tmp/wd", model="deepseek-v4-flash",
                timeout_s=240, adapters_dir="/repo/obench/adapters",
                image="openbench-harness:latest",
                instruction_path="/tmp/instr.txt",
            )
        finally:
            os.environ.clear()
            os.environ.update(orig_env)
        self.assertIn("OPENROUTER_API_KEY", cmd)
        self.assertNotIn("DEEPSEEK_API_KEY", cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", cmd)
        self.assertNotIn("CURSOR_API_KEY", cmd)
        self.assertNotIn("MOONSHOT_API_KEY", cmd)
        self.assertIn("ANTHROPIC_API_KEY", claude_cmd)
        self.assertNotIn("CURSOR_API_KEY", claude_cmd)
        self.assertIn("CURSOR_API_KEY", cursor_cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", cursor_cmd)
        self.assertIn("ANTHROPIC_API_KEY=openbench-bridge-placeholder", codex_opus_cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", codex_opus_cmd)
        self.assertNotIn("CURSOR_API_KEY", codex_opus_cmd)
        self.assertIn("OPENROUTER_API_KEY", grokbuild_cmd)  # same routing on every harness
        self.assertNotIn("ANTHROPIC_API_KEY", grokbuild_cmd)
        self.assertNotIn("CURSOR_API_KEY", grokbuild_cmd)
        for argv in (cmd, claude_cmd, cursor_cmd, codex_opus_cmd, grokbuild_cmd):
            for secret in ("sk-test", "sk-ant-test", "cursor-test"):
                self.assertFalse(any(secret in a for a in argv),
                                 "secret value must not appear in argv")

    def test_grokbuild_sol_forwards_cliproxy_route_and_ingress_key_by_name(self):
        env = {
            "CLIPROXYAPI_BASE_URL": "http://127.0.0.1:8317/v1",
            "CLIPROXYAPI_API_KEY": "secret-ingress-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cmd = docker_exec.build_docker_cmd(
                harness="grokbuild", workdir="/tmp/wd", model="gpt-5.6-sol",
                timeout_s=240, adapters_dir="/repo/obench/adapters", image="image",
                instruction_path="/tmp/instr.txt")
        self.assertIn("CLIPROXYAPI_BASE_URL", cmd)
        self.assertIn("CLIPROXYAPI_API_KEY", cmd)
        self.assertFalse(any("secret-ingress-key" in arg for arg in cmd))

    def test_auth_mount_readonly_when_present(self):
        # Use a fake HOME so the test is deterministic regardless of the host.
        home = tempfile.mkdtemp(prefix="fake_home_")
        try:
            # codex mounts individual auth files, never the whole ~/.codex
            # (which also holds multi-GB worktrees/sessions). codex_v1/v2 reuse
            # that same staged auth and compose their own CODEX_HOME at runtime.
            os.makedirs(os.path.join(home, ".codex"))
            with open(os.path.join(home, ".codex", "auth.json"), "w") as fh:
                fh.write("{}")
            orig = os.path.expanduser
            os.path.expanduser = lambda p: home if p == "~" else orig(p)
            try:
                variants = {h: docker_exec._auth_mount_args(h) for h in ("codex", "codex_v1", "codex_v2")}
            finally:
                os.path.expanduser = orig
            for harness, args in variants.items():
                with self.subTest(harness=harness):
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

    def test_auth_return_mount_is_writable_and_scoped_to_declared_harness(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as returned:
            auth = os.path.join(home, ".pi", "agent", "auth.json")
            os.makedirs(os.path.dirname(auth))
            with open(auth, "wb") as fh:
                fh.write(b"synthetic")
            original = os.path.expanduser
            os.path.expanduser = lambda value: home if value == "~" else original(value)
            try:
                cmd = docker_exec.build_docker_cmd(
                    harness="pi", workdir="/tmp/wd", model="gpt-5.5-medium",
                    timeout_s=240, adapters_dir="/repo/obench/adapters", image="image",
                    instruction_path="/tmp/instr", auth_return_dir=returned,
                )
                undeclared = docker_exec.build_docker_cmd(
                    harness="claude", workdir="/tmp/wd", model="claude-opus-4-8",
                    timeout_s=240, adapters_dir="/repo/obench/adapters", image="image",
                    instruction_path="/tmp/instr", auth_return_dir=returned,
                )
            finally:
                os.path.expanduser = original
            self.assertIn(f"{returned}:/bench/auth-return:rw", cmd)
            self.assertIn("BENCH_AUTH_PERSIST_HARNESS=pi", cmd)
            self.assertIn("auth_persist.py:/bench/auth_persist.py:ro", " ".join(cmd))
            self.assertNotIn("/bench/auth-return", " ".join(undeclared))

    def test_grok_container_auth_is_the_persist_master(self):
        with tempfile.TemporaryDirectory() as home:
            dedicated = os.path.join(home, ".openbench", "grok-container-auth", "auth.json")
            fallback = os.path.join(home, ".grok", "auth.json")
            os.makedirs(os.path.dirname(dedicated))
            os.makedirs(os.path.dirname(fallback))
            for path in (dedicated, fallback):
                with open(path, "wb") as fh:
                    fh.write(b"synthetic")
            original = os.path.expanduser
            os.path.expanduser = lambda value: home if value == "~" else original(value)
            try:
                targets = docker_exec._auth_persist_targets("grokbuild")
            finally:
                os.path.expanduser = original
            self.assertEqual(targets, [(dedicated, ".grok/auth.json")])

    def test_codex_ablation_mounts_only_variant_dir(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}):
            for harness, variant in (("codex_v1", "v1"), ("codex_v2", "v2")):
                with self.subTest(harness=harness):
                    cmd = docker_exec.build_docker_cmd(
                        harness=harness, workdir="/tmp/wd", model="deepseek-v4-flash",
                        timeout_s=240, adapters_dir="/repo/obench/adapters",
                        image="openbench-harness:latest",
                        instruction_path="/tmp/instr.txt",
                    )
                    joined = " ".join(cmd)
                    expected = f"/ablation/codex-home-{variant}:/bench/ablation/codex-home-{variant}:ro"
                    self.assertIn(expected, joined)
                    self.assertNotIn("/ablation:/bench/ablation:ro", joined)
                    self.assertIn("OPENROUTER_API_KEY=openbench-bridge-placeholder", cmd)
                    self.assertNotIn("DEEPSEEK_API_KEY", cmd)

    def test_grokbuild_mounts_only_grok_auth_file(self):
        # The native xAI subscription lane (grok-4.5) needs ~/.grok/auth.json in
        # the container; nothing else from ~/.grok may be mounted.
        home = tempfile.mkdtemp(prefix="fake_home_")
        try:
            os.makedirs(os.path.join(home, ".grok"))
            auth_path = os.path.join(home, ".grok", "auth.json")
            with open(auth_path, "w") as fh:
                fh.write("{}")
            with open(os.path.join(home, ".grok", "config.toml"), "w") as fh:
                fh.write("")
            orig = os.path.expanduser
            os.path.expanduser = lambda p: home if p == "~" else orig(p)
            try:
                self.assertEqual(docker_exec._auth_mount_args("grokbuild"), [
                    "-v", f"{auth_path}:/bench/auth/.grok/auth.json:ro",
                ])
                self.assertNotIn("config.toml", " ".join(
                    docker_exec._auth_mount_args("grokbuild")))
            finally:
                os.path.expanduser = orig
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    def test_opencode_mounts_new_data_auth_path(self):
        home = tempfile.mkdtemp(prefix="fake_home_")
        try:
            os.makedirs(os.path.join(home, ".opencode", "data"))
            with open(os.path.join(home, ".opencode", "data", "auth.json"), "w") as fh:
                fh.write("{}")
            orig = os.path.expanduser
            os.path.expanduser = lambda p: home if p == "~" else orig(p)
            try:
                args = docker_exec._auth_mount_args("opencode")
            finally:
                os.path.expanduser = orig
            self.assertTrue(
                any(a.endswith(".opencode/data/auth.json:/bench/auth/.opencode/data/auth.json:ro")
                    for a in args),
                f"expected opencode auth-only mount, got {args}")
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    def test_cursor_container_auth_maps_to_linux_home_paths(self):
        home = tempfile.mkdtemp(prefix="fake_home_")
        try:
            os.makedirs(os.path.join(home, ".openbench", "cursor-container-auth",
                                     ".config", "cursor"))
            os.makedirs(os.path.join(home, ".openbench", "cursor-container-auth",
                                     ".cursor"))
            with open(os.path.join(home, ".openbench", "cursor-container-auth",
                                   ".config", "cursor", "auth.json"), "w") as fh:
                fh.write("{}")
            orig = os.path.expanduser
            os.path.expanduser = lambda p: home if p == "~" else orig(p)
            try:
                args = docker_exec._auth_mount_args("cursor")
            finally:
                os.path.expanduser = orig
            self.assertTrue(
                any(a.endswith(".openbench/cursor-container-auth/.config/cursor/auth.json:/bench/auth/.config/cursor/auth.json:ro")
                    for a in args),
                f"expected Linux cursor auth-only mount, got {args}")
            self.assertFalse(any("/.cursor:" in a for a in args),
                             f"personal Cursor CLI config must not be mounted: {args}")
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    def test_declarative_candidate_mounts_spec_runtime_and_auth(self):
        with tempfile.TemporaryDirectory() as home:
            auth = os.path.join(home, ".cli", "auth.json")
            os.makedirs(os.path.dirname(auth))
            with open(auth, "w", encoding="utf-8") as fh:
                fh.write("{}")
            spec = os.path.join(home, "candidate", "harness.toml")
            os.makedirs(os.path.dirname(spec))
            with open(spec, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="mine"\ncommand=["cli", "{prompt}"]\n')
            original = os.path.expanduser
            os.path.expanduser = lambda value: value.replace("~", home, 1) if value.startswith("~") else value
            try:
                cmd = docker_exec.build_docker_cmd(
                    harness="mine", workdir="/tmp/wd", model="model", timeout_s=9,
                    adapters_dir="/repo/obench/adapters", image="image",
                    instruction_path="/tmp/instruction", candidate_path=spec,
                    candidate_auth_files=[{"source": "~/.cli/auth.json", "destination": ".cli/auth.json"}],
                    candidate_config_dir=os.path.join(home, "external-config"),
                )
            finally:
                os.path.expanduser = original
            joined = " ".join(cmd)
            self.assertIn(f"{spec}:/bench/candidate.toml:ro", joined)
            self.assertNotIn(f"{os.path.dirname(spec)}:/bench/candidate:ro", joined)
            self.assertIn("candidates.py:/bench/candidates.py:ro", joined)
            self.assertIn(f"{home}/external-config:/bench/candidate-config:ro", joined)
            self.assertIn("OPENBENCH_CANDIDATE_CONFIG_DIR=/bench/candidate-config", cmd)
            self.assertIn(f"{auth}:/bench/auth/.cli/auth.json:ro", joined)
            self.assertEqual(cmd[-1], "/bench/candidate.toml")

    def test_manifest_stock_like_label_grants_no_stock_credentials(self):
        with mock.patch.object(docker_exec, "_auth_mount_args", return_value=[]) as auth_mounts:
            docker_exec.build_docker_cmd(
                harness="codex", workdir="/tmp/wd", model="gpt-5.5-medium", timeout_s=9,
                adapters_dir="/repo/obench/adapters", image="image",
                instruction_path="/tmp/instruction",
                candidate_path="/tmp/harness.toml", base_harness=None,
            )
        auth_mounts.assert_called_once_with(None)
        legacy_label = docker_exec.build_docker_cmd(
            harness="codex_v1", workdir="/tmp/wd", model="model", timeout_s=9,
            adapters_dir="/repo/obench/adapters", image="image",
            instruction_path="/tmp/instruction", candidate_path="/tmp/harness.toml",
        )
        self.assertNotIn("/bench/ablation/codex-home-v1", " ".join(legacy_label))

    def test_candidate_pass_env_is_name_only(self):
        with mock.patch.dict(os.environ, {"BYO_API_KEY": "secret-value"}):
            cmd = docker_exec.build_docker_cmd(
                harness="mine", workdir="/tmp/wd", model="model", timeout_s=9,
                adapters_dir="/repo/obench/adapters", image="image",
                instruction_path="/tmp/instruction",
                candidate_pass_env=["BYO_API_KEY"],
            )
        self.assertIn("BYO_API_KEY", cmd)
        self.assertFalse(any("secret-value" in part for part in cmd))
        with mock.patch.dict(os.environ, {"EMPTY_SETTING": ""}):
            empty = docker_exec.build_docker_cmd(
                harness="mine", workdir="/tmp/wd", model="model", timeout_s=9,
                adapters_dir="/repo/obench/adapters", image="image",
                instruction_path="/tmp/instruction",
                candidate_pass_env=["EMPTY_SETTING"],
            )
        self.assertIn("EMPTY_SETTING", empty)

        with mock.patch.dict(os.environ, {"INHERITED_SETTING": "private-value"}):
            inherited = docker_exec.build_docker_cmd(
                harness="mine", workdir="/tmp/wd", model="model", timeout_s=9,
                adapters_dir="/repo/obench/adapters", image="image",
                instruction_path="/tmp/instruction", candidate_inherit_env=True,
            )
        self.assertIn("INHERITED_SETTING", inherited)
        self.assertFalse(any("private-value" in part for part in inherited))
        self.assertIn("HOME=/root", inherited)
        self.assertNotIn("HOME", inherited)

        proxy = {
            "OPENBENCH_PROXY_BASE_URL": "http://host-value",
            "OPENBENCH_PROXY_CELL_TOKEN": "host-token",
        }
        explicit = {
            "OPENBENCH_PROXY_BASE_URL": "http://per-cell",
            "OPENBENCH_PROXY_CELL_TOKEN": "cell-token",
        }
        with mock.patch.dict(os.environ, proxy):
            routed = docker_exec.build_docker_cmd(
                harness="mine", workdir="/tmp/wd", model="model", timeout_s=9,
                adapters_dir="/repo/obench/adapters", image="image",
                instruction_path="/tmp/instruction", candidate_inherit_env=True,
                extra_env=explicit,
            )
        self.assertIn("OPENBENCH_PROXY_BASE_URL=http://per-cell", routed)
        self.assertNotIn("OPENBENCH_PROXY_BASE_URL", routed)
        self.assertNotIn("OPENBENCH_PROXY_CELL_TOKEN", routed)


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

    A wedged inner CLI can keep the docker client alive past the adapter
    timeout. These tests fake Popen so no Docker daemon is needed and prove our
    own deadline loop removes the named container instead of relying on
    subprocess.run(..., timeout=...).
    """

    class FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, stdin=None,
                     *, stdout_text="", stderr_text="", polls_before_exit=0,
                     returncode=0, raise_on_start=None):
            if raise_on_start:
                raise raise_on_start
            self.cmd = cmd
            self.returncode = returncode
            self.polls_before_exit = polls_before_exit
            self.terminated = False
            self.killed = False
            if stdout_text:
                stdout.write(stdout_text.encode("utf-8"))
            if stderr_text:
                stderr.write(stderr_text.encode("utf-8"))

        def poll(self):
            if self.terminated or self.killed:
                self.returncode = -15 if self.terminated else -9
                return self.returncode
            if self.polls_before_exit <= 0:
                return self.returncode
            self.polls_before_exit -= 1
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            if self.poll() is None:
                raise subprocess.TimeoutExpired(self.cmd, timeout)
            return self.returncode

    def _patch(self, popen_factory):
        """Replace docker_exec subprocess calls; return the recorded call log."""
        calls = []

        class FakeRunProc:
            def __init__(self, stdout="", returncode=0):
                self.stdout, self.stderr, self.returncode = stdout, "", returncode

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeRunProc()  # rm -f / ps probe: container gone

        def fake_popen(cmd, **kwargs):
            calls.append(cmd)
            return popen_factory(cmd, **kwargs)

        self._orig = (docker_exec.subprocess.run, docker_exec.subprocess.Popen,
                      docker_exec.preflight, docker_exec._TIMEOUT_GRACE_S)
        docker_exec.subprocess.run = fake_run
        docker_exec.subprocess.Popen = fake_popen
        docker_exec.preflight = lambda image: None
        docker_exec._TIMEOUT_GRACE_S = 0
        self.addCleanup(self._unpatch)
        return calls

    def _unpatch(self):
        (docker_exec.subprocess.run, docker_exec.subprocess.Popen,
         docker_exec.preflight, docker_exec._TIMEOUT_GRACE_S) = self._orig

    def _invoke(self, timeout_s=900):
        return docker_exec.run_in_container(
            "pi", "do it", "/tmp/wd", "deepseek-v4-flash", timeout_s,
            "/repo/obench/adapters")

    def _rm_calls(self, calls):
        return [c for c in calls if c[:3] == ["docker", "rm", "-f"]]

    def test_watchdog_timeout_removes_container_and_kills_client(self):
        fake_holder = []

        def factory(cmd, **kwargs):
            proc = self.FakePopen(
                cmd, stdout_text="partial", polls_before_exit=10_000, **kwargs)
            fake_holder.append(proc)
            return proc

        calls = self._patch(factory)
        result = self._invoke(timeout_s=0.05)
        self.assertFalse(result["completed"])
        self.assertIn("timeout", result["error"])
        self.assertEqual(result["output_tail"], "partial")
        self.assertTrue(fake_holder[0].terminated)
        self.assertEqual(len(self._rm_calls(calls)), 2)  # watchdog + final sweep
        self.assertGreaterEqual(result["host_wall_time_s"], 0)

    def test_recovered_final_cleanup_preserves_timeout_result(self):
        self._patch(lambda cmd, **kwargs: self.FakePopen(
            cmd, stdout_text="partial", polls_before_exit=10_000, **kwargs))
        orig_force = docker_exec._force_remove_container
        force_results = iter([False, True])
        docker_exec._force_remove_container = lambda name: next(force_results)
        try:
            result = self._invoke(timeout_s=0.05)
        finally:
            docker_exec._force_remove_container = orig_force
        self.assertFalse(result["completed"])
        self.assertIn("timeout", result["error"])
        self.assertNotIn("cleanup failed", result["error"])

    def test_unexpected_exception_still_removes_container(self):
        def factory(cmd, **kwargs):
            return self.FakePopen(
                cmd, raise_on_start=RuntimeError("daemon hiccup"), **kwargs)

        calls = self._patch(factory)
        with self.assertRaises(RuntimeError):
            self._invoke()
        self.assertEqual(len(self._rm_calls(calls)), 1)

    def test_clean_return_also_sweeps_name(self):
        # Even when `docker run --rm` exits normally, removal-by-name fires as
        # a cheap no-op so a glitched --rm can't leave a wedged container.
        sentinel = docker_exec.RESULT_SENTINEL + ' {"completed": true}\n'

        calls = self._patch(
            lambda cmd, **kwargs: self.FakePopen(cmd, stdout_text=sentinel, **kwargs))
        result = self._invoke()
        self.assertTrue(result["completed"])
        self.assertIn("host_wall_time_s", result)
        self.assertEqual(len(self._rm_calls(calls)), 1)

    def test_container_observer_receives_killable_name_before_launch(self):
        sentinel = docker_exec.RESULT_SENTINEL + ' {"completed": true}\n'
        calls = self._patch(
            lambda cmd, **kwargs: self.FakePopen(
                cmd, stdout_text=sentinel, **kwargs))
        observed = []
        result = docker_exec.run_in_container(
            "pi",
            "do it",
            "/tmp/wd",
            "deepseek-v4-flash",
            900,
            "/repo/obench/adapters",
            container_observer=observed.append,
        )
        self.assertTrue(result["completed"])
        self.assertEqual(len(observed), 1)
        docker_run = next(call for call in calls if call[:2] == ["docker", "run"])
        self.assertIn(observed[0], docker_run)
        self.assertTrue(
            any(call[-1] == observed[0] for call in self._rm_calls(calls)))

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
    def test_manifest_proxy_metadata_does_not_select_stock_auth(self):
        candidate = SimpleNamespace(
            path="/tmp/harness.toml", kind="manifest", base_adapter=None,
            proxy_adapter="codex", auth_files=[], pass_env=[], inherit_env=False,
            spec_bytes=b'kind="manifest"\nname="mine"\ncommand=["cli"]\n',
        )
        result = {"completed": True}
        with mock.patch.object(docker_exec, "run_in_container", return_value=result) as run_container:
            actual, lane = run.invoke_adapter(
                "docker", "mine", "prompt", "/tmp/work", "model", 9,
                "/tmp/adapters", "image", False, candidate=candidate,
            )
        self.assertIs(actual, result)
        self.assertEqual(lane, "docker")
        self.assertIsNone(run_container.call_args.kwargs["base_harness"])


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
            self.assertIn("host_env_setup_s", result)
            self.assertIn("host_agent_wall_time_s", result)
            self.assertGreaterEqual(result["host_env_setup_s"], 0)
            self.assertGreaterEqual(result["host_agent_wall_time_s"], 0)
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
            with self.assertRaises(docker_exec.DockerUnavailable) as cm:
                run.invoke_adapter(
                    "docker", "fake_adapter", "x", tempfile.mkdtemp(),
                    "gpt-5.5-medium", 30, FIXTURES_DIR,
                    "img:latest", docker_fallback=False,
                )
            self.assertEqual(getattr(cm.exception, "bench_exec_used"), "docker")
            self.assertGreaterEqual(getattr(cm.exception, "bench_env_setup_s"), 0)
            self.assertEqual(getattr(cm.exception, "bench_agent_wall_time_s"), 0.0)
        finally:
            docker_exec.run_in_container = orig

    def test_docker_setup_exception_is_timed_as_setup_not_agent(self):
        orig = docker_exec.preflight
        docker_exec.preflight = lambda image: (_ for _ in ()).throw(RuntimeError("preflight broke"))
        try:
            with self.assertRaises(RuntimeError) as cm:
                docker_exec.run_in_container(
                    "pi", "do it", "/tmp/wd", "deepseek-v4-flash", 30,
                    "/repo/obench/adapters")
        finally:
            docker_exec.preflight = orig
        self.assertGreaterEqual(getattr(cm.exception, "bench_env_setup_s"), 0)
        self.assertEqual(getattr(cm.exception, "bench_agent_wall_time_s"), 0.0)


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
