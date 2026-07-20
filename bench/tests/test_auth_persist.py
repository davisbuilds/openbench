"""Regression tests for rotating OAuth credential persist-back."""

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")
sys.path.insert(0, BENCH_DIR)

import auth_persist  # noqa: E402
import docker_exec  # noqa: E402
import entry  # noqa: E402


def _load_adapter(name):
    path = os.path.join(ADAPTERS_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("auth_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAtomicPersist(unittest.TestCase):
    def test_rotated_file_atomically_replaces_master_with_mode_0600(self):
        with tempfile.TemporaryDirectory() as td:
            master = os.path.join(td, "auth.json")
            copy = os.path.join(td, "copy.json")
            with open(master, "wb") as fh:
                fh.write(b"old-token")
            with open(copy, "wb") as fh:
                fh.write(b"new-rotated-token")

            real_replace = os.replace
            observed = []

            def inspect_replace(source, destination):
                with open(master, "rb") as fh:
                    old_during_replace = fh.read()
                with open(source, "rb") as fh:
                    complete_temp = fh.read()
                observed.append((old_during_replace, complete_temp,
                                 stat.S_IMODE(os.stat(source).st_mode)))
                real_replace(source, destination)

            with mock.patch.object(auth_persist.os, "replace", side_effect=inspect_replace):
                self.assertTrue(auth_persist.persist_auth_file(copy, master))

            self.assertEqual(observed, [(b"old-token", b"new-rotated-token", 0o600)])
            with open(master, "rb") as fh:
                self.assertEqual(fh.read(), b"new-rotated-token")
            self.assertEqual(stat.S_IMODE(os.stat(master).st_mode), 0o600)

    def test_identical_file_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            master = os.path.join(td, "auth.json")
            copy = os.path.join(td, "copy.json")
            for path in (master, copy):
                with open(path, "wb") as fh:
                    fh.write(b"same")
            before = os.stat(master)
            with mock.patch.object(auth_persist.os, "replace") as replace:
                self.assertFalse(auth_persist.persist_auth_file(copy, master))
            replace.assert_not_called()
            after = os.stat(master)
            self.assertEqual((before.st_ino, before.st_mtime_ns),
                             (after.st_ino, after.st_mtime_ns))


class TestLocalAdapterPersist(unittest.TestCase):
    def test_pi_persists_rotation_even_when_cli_fails(self):
        pi = _load_adapter("pi")
        with tempfile.TemporaryDirectory() as td:
            master = os.path.join(td, "auth.json")
            with open(master, "w", encoding="utf-8") as fh:
                fh.write('{"openai-codex":{"refresh":"old"}}')
            pi._REAL_AUTH = master

            def failed_run(*args, **kwargs):
                isolated = os.path.join(kwargs["env"]["PI_CODING_AGENT_DIR"], "auth.json")
                with open(isolated, "w", encoding="utf-8") as fh:
                    fh.write('{"openai-codex":{"refresh":"rotated"}}')
                return SimpleNamespace(returncode=1, stdout="", stderr="failed")

            with mock.patch.object(pi.subprocess, "run", side_effect=failed_run):
                result = pi.run("task", td, "gpt-5.5-medium", 10)
            self.assertFalse(result["completed"])
            with open(master, encoding="utf-8") as fh:
                self.assertIn("rotated", fh.read())


class TestDockerPersistPlumbing(unittest.TestCase):
    def test_build_command_mounts_writable_return_directory(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as returned:
            auth = os.path.join(home, ".pi", "agent", "auth.json")
            os.makedirs(os.path.dirname(auth))
            with open(auth, "w", encoding="utf-8") as fh:
                fh.write("{}")
            original = os.path.expanduser
            with mock.patch.object(os.path, "expanduser",
                                   side_effect=lambda p: home if p == "~" else original(p)):
                cmd = docker_exec.build_docker_cmd(
                    "pi", "/tmp/work", "gpt-5.5-medium", 10, ADAPTERS_DIR,
                    "image", "/tmp/instruction", auth_return_dir=returned)
            joined = " ".join(cmd)
            self.assertIn(f"{returned}:{docker_exec.AUTH_RETURN}:rw", joined)
            self.assertIn("auth_persist.py:/bench/auth_persist.py:ro", joined)
            self.assertIn("BENCH_AUTH_PERSIST_HARNESS=pi", cmd)

    def test_entry_returns_only_declared_auth_file(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as returned:
            auth = os.path.join(home, ".pi", "agent", "auth.json")
            other = os.path.join(home, ".pi", "agent", "settings.json")
            os.makedirs(os.path.dirname(auth))
            with open(auth, "wb") as fh:
                fh.write(b"rotated")
            with open(other, "wb") as fh:
                fh.write(b"private config")
            with mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(entry, "AUTH_RETURN", returned):
                entry._return_auth("pi")
            with open(os.path.join(returned, ".pi", "agent", "auth.json"), "rb") as fh:
                self.assertEqual(fh.read(), b"rotated")
            self.assertFalse(os.path.exists(
                os.path.join(returned, ".pi", "agent", "settings.json")))


if __name__ == "__main__":
    unittest.main()
