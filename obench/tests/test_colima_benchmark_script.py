#!/usr/bin/env python3
"""Tests for scripts/local/colima-benchmark.sh against stub colima/docker/brew.

The stubs log every invocation, and `colima` answers `list --json` and
`status` the way colima 0.10 does: `status` names no CPU or memory figures, so
resources can only come from `list --json`.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local" / "colima-benchmark.sh"
GIB = 1024 ** 3

COLIMA_STUB = r"""#!/bin/sh
echo "colima $*" >> "$STUB_LOG"
case "$1" in
  list)
    # Like colima 0.10: listing a profile that does not exist yet fails.
    case " $* " in
      *" --profile "*)
        profile=$(echo " $* " | sed 's/.* --profile \([^ ]*\) .*/\1/')
        grep -q "\"name\": *\"$profile\"" "$STUB_DIR/list.json" || exit 1 ;;
    esac
    cat "$STUB_DIR/list.json" ;;
  status)
    if grep -q '"status": *"Running"' "$STUB_DIR/list.json"; then
      echo 'level=info msg="colima is running using macOS Virtualization.Framework"' >&2
      echo 'level=info msg="runtime: docker"' >&2
    else
      echo 'level=fatal msg="colima is not running"' >&2
      exit 1
    fi ;;
  stop) sed -i.bak 's/"Running"/"Stopped"/' "$STUB_DIR/list.json" ;;
esac
exit 0
"""

DOCKER_STUB = r"""#!/bin/sh
echo "docker DOCKER_CONTEXT=${DOCKER_CONTEXT:-} $*" >> "$STUB_LOG"
case "$1 $2" in
  "buildx version") echo "github.com/docker/buildx v0.38.0" ;;
  "context show") echo "colima" ;;
esac
case "$1" in
  info) echo "29.5.2" ;;
  run) echo "Hello from Docker!" ;;
esac
exit 0
"""

BREW_STUB = r"""#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
exit 0
"""


class ColimaBenchmarkScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, body in (("colima", COLIMA_STUB), ("docker", DOCKER_STUB), ("brew", BREW_STUB)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        self.log = self.dir / "calls.log"
        self.log.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def running(self, profile, cpus, memory_gib):
        record = {"name": profile, "status": "Running", "arch": "aarch64", "cpus": cpus,
                  "memory": memory_gib * GIB, "disk": 100 * GIB, "runtime": "docker"}
        (self.dir / "list.json").write_text(json.dumps(record) + "\n")

    def stopped(self):
        (self.dir / "list.json").write_text("")

    def run_script(self, *args, env=None):
        environ = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.dir),
                   "STUB_LOG": str(self.log), "STUB_DIR": str(self.dir)}
        environ.update(env or {})
        result = subprocess.run(["bash", str(SCRIPT), *args], env=environ,
                                capture_output=True, text=True, timeout=60)
        return result, self.log.read_text().splitlines()

    def colima_calls(self, calls, verb):
        return [c for c in calls if c.startswith(f"colima {verb}")]

    def test_vm_meeting_the_floor_is_left_running(self):
        self.running("default", 4, 12)
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.colima_calls(calls, "stop"), [])
        self.assertEqual(self.colima_calls(calls, "start"), [])

    def test_undersized_vm_is_restarted_with_the_pinned_floor(self):
        self.running("default", 4, 6)
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.colima_calls(calls, "stop")), 1)
        (start,) = self.colima_calls(calls, "start")
        self.assertIn("--cpu 4", start)
        self.assertIn("--memory 12", start)

    def test_named_profile_targets_only_its_own_vm_and_context(self):
        self.stopped()
        result, calls = self.run_script("--profile", "openbench")
        self.assertEqual(result.returncode, 0, result.stderr)
        (start,) = self.colima_calls(calls, "start")
        self.assertIn("--profile openbench", start)
        # Starting a second profile must not take over the current context.
        self.assertIn("--activate=false", start)
        for call in calls:
            if call.startswith(("colima start", "colima stop", "colima status")):
                self.assertIn("--profile openbench", call)
            elif call.startswith("docker ") and " context " not in call:
                self.assertIn("DOCKER_CONTEXT=colima-openbench ", call)
        self.assertIn("export DOCKER_CONTEXT=colima-openbench", result.stdout)

    def test_status_of_a_profile_not_yet_created(self):
        self.running("default", 4, 6)
        result, calls = self.run_script("--status", "--profile", "openbench")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'openbench' is NOT running", result.stdout)
        self.assertEqual(self.colima_calls(calls, "start"), [])

    def test_profile_can_come_from_the_environment(self):
        self.stopped()
        result, calls = self.run_script(env={"OBENCH_COLIMA_PROFILE": "openbench"})
        self.assertEqual(result.returncode, 0, result.stderr)
        (start,) = self.colima_calls(calls, "start")
        self.assertIn("--profile openbench", start)

    def test_autostart_is_opt_in(self):
        self.running("default", 4, 12)
        _, calls = self.run_script()
        self.assertFalse([c for c in calls if c.startswith("brew services")])
        _, calls = self.run_script("--autostart")
        self.assertIn("brew services start colima", calls)

    def test_autostart_refuses_a_named_profile(self):
        # brew's colima service starts only the default profile.
        self.stopped()
        result, calls = self.run_script("--profile", "openbench", "--autostart")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.colima_calls(calls, "start"), [])
        self.assertFalse([c for c in calls if c.startswith("brew")])


if __name__ == "__main__":
    unittest.main()
