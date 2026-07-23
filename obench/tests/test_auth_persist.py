"""Regression tests for rotating OAuth credential persist-back."""

import importlib.util
import os

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")
from obench import auth_persist  # noqa: E402
from obench import docker_exec  # noqa: E402
from obench import entry  # noqa: E402


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
                fh.write(b'{"provider":"xai","refresh_token":"old-token"}')
            with open(copy, "wb") as fh:
                fh.write(b'{"provider":"xai","refresh_token":"new-rotated-token"}')

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

            self.assertEqual(observed, [(
                b'{"provider":"xai","refresh_token":"old-token"}',
                b'{"provider":"xai","refresh_token":"new-rotated-token"}', 0o600)])
            with open(master, "rb") as fh:
                self.assertEqual(
                    fh.read(), b'{"provider":"xai","refresh_token":"new-rotated-token"}')
            self.assertEqual(stat.S_IMODE(os.stat(master).st_mode), 0o600)

    def test_symlinked_master_updates_target_without_replacing_link(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "canonical.json")
            master = os.path.join(td, "auth.json")
            copy = os.path.join(td, "copy.json")
            with open(target, "wb") as fh:
                fh.write(b'{"provider":"xai","refresh_token":"old"}')
            os.symlink(target, master)
            with open(copy, "wb") as fh:
                fh.write(b'{"provider":"xai","refresh_token":"new"}')
            self.assertTrue(auth_persist.persist_auth_file(copy, master))
            self.assertTrue(os.path.islink(master))
            with open(target, "rb") as fh:
                self.assertIn(b'"new"', fh.read())

    def test_account_identity_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            master = os.path.join(td, "auth.json")
            copy = os.path.join(td, "copy.json")
            with open(master, "wb") as fh:
                fh.write(b'{"account_id":"owner","refresh_token":"old"}')
            with open(copy, "wb") as fh:
                fh.write(b'{"account_id":"attacker","refresh_token":"new"}')
            with self.assertRaises(ValueError):
                auth_persist.persist_auth_file(copy, master)
            with open(master, "rb") as fh:
                self.assertIn(b'"owner"', fh.read())

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
            self.assertFalse(os.path.exists(master + ".lock"))

    def test_best_effort_wrapper_does_not_mask_persist_failure(self):
        with mock.patch.object(auth_persist, "persist_auth_file",
                               side_effect=PermissionError("synthetic")), \
                mock.patch.object(auth_persist.sys, "stderr"):
            self.assertFalse(auth_persist.try_persist_auth_file("copy", "master"))


class TestLocalAdapterPersist(unittest.TestCase):
    def test_pi_persists_rotation_even_when_cli_fails(self):
        pi = _load_adapter("pi")
        with tempfile.TemporaryDirectory() as td:
            master = os.path.join(td, "auth.json")
            with open(master, "w", encoding="utf-8") as fh:
                fh.write('{"openai-codex":{"refresh":"old"}}')
            pi._REAL_AUTH = master

            def failed_run(cmd, cwd, timeout_s, env):
                isolated = os.path.join(env["PI_CODING_AGENT_DIR"], "auth.json")
                with open(isolated, "w", encoding="utf-8") as fh:
                    fh.write('{"openai-codex":{"refresh":"rotated"}}')
                return "", "failed", 1, False

            with mock.patch.object(pi, "_run_streaming", side_effect=failed_run):
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

    def test_candidate_persist_auth_mounts_return_dir(self):
        with tempfile.TemporaryDirectory() as returned:
            cmd = docker_exec.build_docker_cmd(
                "third-party", "/tmp/work", "gpt-5.5-medium", 10, ADAPTERS_DIR,
                "image", "/tmp/instruction",
                candidate_path="/tmp/candidate.toml",
                candidate_persist_auth=True,
                auth_return_dir=returned)
            self.assertIn(f"{returned}:{docker_exec.AUTH_RETURN}:rw", " ".join(cmd))
            self.assertIn("BENCH_AUTH_PERSIST_CANDIDATE=1", cmd)
            self.assertNotIn("BENCH_AUTH_PERSIST_HARNESS=", " ".join(cmd))

    def test_candidate_auth_persist_targets_default_empty(self):
        from obench.candidates import candidate_auth_persist_targets
        fake = SimpleNamespace(persist_auth=False, auth_files=[
            {"source": "~/.x/auth.json", "destination": ".x/auth.json"}])
        self.assertEqual(candidate_auth_persist_targets(fake), [])

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
