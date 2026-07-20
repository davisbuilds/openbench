#!/usr/bin/env python3
"""Tests for per-phase timing fields on runner rows."""

import os
import shutil
import stat
import sys
import tempfile
import time
import unittest


from obench import run  # noqa: E402


class TestPhaseTiming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_phase_timing_")
        self.tasks_dir = os.path.join(self.tmp, "tasks")
        self.adapters_dir = os.path.join(self.tmp, "adapters")
        self.task = "timed-task"
        task_dir = os.path.join(self.tasks_dir, self.task)
        os.makedirs(os.path.join(task_dir, "workspace"))
        os.makedirs(self.adapters_dir)
        with open(os.path.join(task_dir, "instruction.md"), "w", encoding="utf-8") as fh:
            fh.write("do it\n")
        checker = os.path.join(task_dir, "checker.sh")
        with open(checker, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(checker, os.stat(checker).st_mode | stat.S_IXUSR)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_with_fakes(self, exec_used="local", extra_result=None):
        orig_invoke, orig_checker = run.invoke_adapter, run.run_checker

        def fake_invoke(*args, **kwargs):
            time.sleep(0.01)
            result = {
                "completed": True,
                "error": None,
                "output_tail": "",
                "tokens": None,
                "turns": None,
                "cmd": ["fake"],
            }
            if extra_result:
                result.update(extra_result)
            return result, exec_used

        def fake_checker(*args, **kwargs):
            time.sleep(0.01)
            return 0, None, "", ""

        try:
            run.invoke_adapter = fake_invoke
            run.run_checker = fake_checker
            return run.run_cell(
                "fake", self.task, "m", 1, 60,
                self.tasks_dir, self.adapters_dir, 30,
            )
        finally:
            run.invoke_adapter, run.run_checker = orig_invoke, orig_checker

    def test_stubbed_local_cell_populates_phase_fields(self):
        row = self._run_with_fakes()

        self.assertIsInstance(row["t_env_setup_s"], (int, float))
        self.assertGreaterEqual(row["t_env_setup_s"], 0)
        self.assertGreater(row["t_agent_s"], 0)
        self.assertGreater(row["t_checker_s"], 0)
        self.assertEqual(row["wall_time_s"], row["t_agent_s"])

    def test_stubbed_docker_cell_uses_host_phase_timings(self):
        row = self._run_with_fakes(
            exec_used="docker",
            extra_result={
                "host_env_setup_s": 1.25,
                "host_agent_wall_time_s": 2.5,
                "host_wall_time_s": 3.5,
            },
        )

        self.assertEqual(row["exec_mode"], "docker")
        self.assertGreaterEqual(row["t_env_setup_s"], 1.25)
        self.assertEqual(row["t_agent_s"], 2.5)
        self.assertEqual(row["wall_time_s"], 3.5)
        self.assertGreater(row["t_checker_s"], 0)


if __name__ == "__main__":
    unittest.main()
