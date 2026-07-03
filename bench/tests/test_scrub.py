#!/usr/bin/env python3
"""Unit + CLI tests for the PII scrubber (bench/scrub.py).

Covers each PII class, idempotency, --check reporting/exit code, that originals
are never modified, tree scrubbing into a parallel dir, and the in-place guard.
A fixed context (synthetic user/home/hostname) keeps results independent of the
machine running the tests.
"""

import os
import subprocess
import sys
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRUB_PY = os.path.join(BENCH_DIR, "scrub.py")

sys.path.insert(0, BENCH_DIR)

import scrub  # noqa: E402

# Synthetic identity so tests don't depend on the real machine.
CTX = scrub.build_context(
    user="alice",
    home="/Users/alice",
    hostnames=["Alices-Laptop.local"],
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write_file(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestScrubClasses(unittest.TestCase):
    def _scrub(self, text):
        return scrub.scrub_text(text, CTX)

    def test_email(self):
        out = self._scrub("contact me at jane.doe+test@example.co.uk please")
        self.assertNotIn("jane.doe", out)
        self.assertIn("<EMAIL>", out)

    def test_home_path(self):
        out = self._scrub("cd /Users/alice/dev/openbench && ls")
        self.assertIn("<HOME>/dev/openbench", out)
        self.assertNotIn("/Users/alice", out)

    def test_alt_home_path_forms(self):
        out = self._scrub("linux path /home/alice/x and mac /Users/alice/y")
        self.assertNotIn("/home/alice", out)
        self.assertNotIn("/Users/alice", out)
        self.assertEqual(out.count("<HOME>"), 2)

    def test_username_standalone(self):
        out = self._scrub("the user alice ran it")
        self.assertIn("<USER>", out)
        self.assertNotIn(" alice ", out)

    def test_username_not_substring(self):
        # Word-boundary: 'alice' inside 'malice' must NOT be replaced.
        out = self._scrub("this was done without malice")
        self.assertIn("malice", out)
        self.assertNotIn("<USER>", out)

    def test_hostname(self):
        out = self._scrub("running on Alices-Laptop.local now")
        self.assertIn("<HOST>", out)
        self.assertNotIn("Alices-Laptop", out)

    def test_api_key(self):
        out = self._scrub("export OPENAI_API_KEY=sk-abcDEF0123456789ghijklmnop")
        self.assertIn("<REDACTED_KEY>", out)
        self.assertNotIn("sk-abcDEF", out)

    def test_github_token(self):
        out = self._scrub("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 used")
        self.assertIn("<REDACTED_KEY>", out)
        self.assertNotIn("ghp_ABCDEF", out)

    def test_aws_key(self):
        out = self._scrub("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE end")
        self.assertIn("<REDACTED_KEY>", out)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_hex_blob(self):
        out = self._scrub("sha " + "a1b2c3d4" * 5 + " done")  # 40 hex chars
        self.assertIn("<REDACTED_HEX>", out)

    def test_base64_blob(self):
        blob = "QWxpY2VJc0FTZWNyZXRWYWx1ZVRoYXRJc1F1aXRlTG9uZ0Vub3VnaA1234"  # >40, has no @
        out = self._scrub("data: " + blob)
        self.assertTrue("<REDACTED_B64>" in out or "<REDACTED_HEX>" in out)

    def test_short_token_not_redacted(self):
        # Short, low-entropy strings must survive (no false positives).
        out = self._scrub("the cat sat on the mat with abc123")
        self.assertEqual(out, "the cat sat on the mat with abc123")

    def test_idempotent(self):
        text = ("user alice at /Users/alice on Alices-Laptop.local, "
                "email jane@example.com, key sk-abcDEF0123456789ghijklmnop")
        once = self._scrub(text)
        twice = self._scrub(once)
        self.assertEqual(once, twice)
        # And a fully-scrubbed string reports zero remaining PII.
        self.assertEqual(scrub.find_pii(once, CTX), [])


class TestFindPii(unittest.TestCase):
    def test_find_reports_categories_and_lines(self):
        text = "line one clean\nemail jane@example.com here\n/Users/alice/x"
        hits = scrub.find_pii(text, CTX)
        cats = {c for c, _ln, _s in hits}
        self.assertIn("email", cats)
        self.assertIn("home-path", cats)
        lines = {ln for _c, ln, _s in hits}
        self.assertIn(2, lines)
        self.assertIn(3, lines)

    def test_find_clean_text_empty(self):
        self.assertEqual(scrub.find_pii("nothing to see here", CTX), [])


class TestFileOps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_scrub_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_scrub_tree_never_modifies_original(self):
        src = os.path.join(self.tmp, "src")
        original = "/Users/alice/secret and jane@example.com"
        self._write("src/a.txt", original)
        out = os.path.join(self.tmp, "out")

        written = scrub.scrub_tree(src, out, CTX)
        self.assertEqual(len(written), 1)
        # Original is byte-for-byte unchanged.
        self.assertEqual(_read(os.path.join(src, "a.txt")), original)
        # Scrubbed copy has placeholders, no PII.
        scrubbed = _read(os.path.join(out, "a.txt"))
        self.assertIn("<HOME>", scrubbed)
        self.assertIn("<EMAIL>", scrubbed)
        self.assertNotIn("/Users/alice", scrubbed)

    def test_scrub_tree_mirrors_nested_layout(self):
        src = os.path.join(self.tmp, "src")
        self._write("src/sub/deep.txt", "at /Users/alice/here")
        out = os.path.join(self.tmp, "out")
        scrub.scrub_tree(src, out, CTX)
        self.assertTrue(os.path.isfile(os.path.join(out, "sub", "deep.txt")))

    def test_scrub_tree_refuses_in_place(self):
        src = os.path.join(self.tmp, "src")
        self._write("src/a.txt", "x")
        with self.assertRaises(ValueError):
            scrub.scrub_tree(src, os.path.join(src, "nested"), CTX)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_scrub_cli_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, SCRUB_PY, *args,
             "--user", "alice", "--home", "/Users/alice",
             "--hostname", "Alices-Laptop.local"],
            capture_output=True, text=True,
        )

    def test_check_exit1_when_pii_present(self):
        f = os.path.join(self.tmp, "t.txt")
        _write_file(f, "email jane@example.com")
        proc = self._run(f, "--check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("email", proc.stdout)
        self.assertIn("POTENTIAL PII", proc.stdout)

    def test_check_exit0_when_clean(self):
        f = os.path.join(self.tmp, "t.txt")
        _write_file(f, "nothing sensitive here")
        proc = self._run(f, "--check")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("CLEAN", proc.stdout)

    def test_check_writes_nothing(self):
        f = os.path.join(self.tmp, "t.txt")
        _write_file(f, "email jane@example.com")
        before = sorted(os.listdir(self.tmp))
        self._run(f, "--check")
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_scrub_mode_writes_copies(self):
        f = os.path.join(self.tmp, "t.txt")
        _write_file(f, "at /Users/alice/x")
        out = os.path.join(self.tmp, "out")
        proc = self._run(f, "--out", out)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("<HOME>", _read(os.path.join(out, "t.txt")))

    def test_scrub_mode_requires_out(self):
        f = os.path.join(self.tmp, "t.txt")
        _write_file(f, "x")
        proc = self._run(f)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
