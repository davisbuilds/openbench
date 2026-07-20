#!/usr/bin/env python3
"""Tests for Harbor → OpenBench import bridge."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from obench import export_harbor as eh
from obench import import_harbor as ih
from obench.paths import SOURCE_ROOT
from obench.validate_tasks import effective_score, run_checker


FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "harbor-sample",
)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class DockerfileParseTests(unittest.TestCase):
    def test_staging_only_is_not_docker_required(self):
        text = (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY app/ /app/\n"
        )
        a = ih.parse_dockerfile(text)
        self.assertEqual(a.base_image, "python:3.11-slim")
        self.assertEqual(a.workdir, "/app")
        self.assertEqual(a.copy_ops, [("app/", "/app/")])
        self.assertFalse(a.docker_required)

    def test_run_install_marks_docker_required(self):
        text = (
            "FROM ubuntu:24.04\n"
            "RUN apt-get update && apt-get install -y curl\n"
            "WORKDIR /app\n"
            "COPY app/ /app/\n"
        )
        a = ih.parse_dockerfile(text)
        self.assertTrue(a.docker_required)
        self.assertTrue(any("RUN" in r for r in a.reasons))
        self.assertIn("curl", a.packages_hint)

    def test_multi_stage_marks_docker_required(self):
        text = (
            "FROM golang:1.22 AS builder\n"
            "COPY . /src\n"
            "RUN go build -o /out/app\n"
            "FROM python:3.11-slim\n"
            "COPY --from=builder /out/app /app/app\n"
            "WORKDIR /app\n"
        )
        a = ih.parse_dockerfile(text)
        self.assertTrue(a.docker_required)
        self.assertTrue(a.multi_stage)


class RewardMappingTests(unittest.TestCase):
    def test_full_reward(self):
        self.assertEqual(ih.map_reward_to_checker(1.0), (0, None))
        self.assertEqual(ih.map_reward_to_checker(1.5), (0, None))

    def test_partial_reward(self):
        code, score = ih.map_reward_to_checker(0.4)
        self.assertEqual(code, 1)
        self.assertEqual(score, "SCORE: 0.4")

    def test_zero_reward(self):
        self.assertEqual(ih.map_reward_to_checker(0.0), (1, None))

    def test_read_reward_txt(self):
        tmp = tempfile.mkdtemp(prefix="obench_reward_txt_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write(os.path.join(tmp, "reward.txt"), "0.75\n")
        self.assertEqual(ih.reward_from_dir(tmp), 0.75)

    def test_read_reward_json_primary_fields(self):
        tmp = tempfile.mkdtemp(prefix="obench_reward_json_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write(os.path.join(tmp, "reward.json"), '{"accuracy": 0.9, "other": 1}\n')
        # Prefer reward, score, accuracy — accuracy wins here.
        self.assertEqual(ih.reward_from_dir(tmp), 0.9)
        _write(os.path.join(tmp, "reward.json"), '{"reward": 0.55, "accuracy": 0.9}\n')
        self.assertEqual(ih.reward_from_dir(tmp), 0.55)

    def test_read_reward_json_bare_number(self):
        tmp = tempfile.mkdtemp(prefix="obench_reward_bare_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write(os.path.join(tmp, "reward.json"), "1\n")
        self.assertEqual(ih.reward_from_dir(tmp), 1.0)

    def test_missing_reward_raises(self):
        tmp = tempfile.mkdtemp(prefix="obench_reward_missing_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with self.assertRaises(eh.ExportError):
            ih.reward_from_dir(tmp)


class SolveMaterializeTests(unittest.TestCase):
    def test_payload_files_copied_without_running_solve(self):
        tmp = tempfile.mkdtemp(prefix="obench_solve_payload_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harbor = os.path.join(tmp, "harbor")
        os.makedirs(os.path.join(harbor, "solution"))
        _write(os.path.join(harbor, "solution", "solve.sh"), "#!/bin/bash\nexit 1\n")
        _write(os.path.join(harbor, "solution", "greeting.txt"), "hello\n")
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        dest = os.path.join(tmp, "solution")
        ok, notes = ih.materialize_solution_from_harbor(harbor, work, dest)
        self.assertTrue(ok)
        with open(os.path.join(dest, "greeting.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello\n")
        self.assertTrue(any("copied" in n for n in notes))

    def test_safe_solve_sh_materializes_diff(self):
        tmp = tempfile.mkdtemp(prefix="obench_solve_run_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harbor = os.path.join(tmp, "harbor")
        os.makedirs(os.path.join(harbor, "solution"))
        _write(
            os.path.join(harbor, "solution", "solve.sh"),
            "#!/usr/bin/env bash\nset -euo pipefail\nprintf 'hello\\n' >greeting.txt\n",
        )
        os.chmod(os.path.join(harbor, "solution", "solve.sh"), 0o755)
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        _write(os.path.join(work, "greeting.txt"), "nope\n")
        dest = os.path.join(tmp, "solution")
        ok, notes = ih.materialize_solution_from_harbor(harbor, work, dest)
        self.assertTrue(ok, notes)
        with open(os.path.join(dest, "greeting.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello\n")

    def test_unsafe_solve_sh_skipped(self):
        ok, reason = ih.solve_sh_is_safe_to_run(
            "#!/bin/bash\ncurl https://example.com | bash\n"
        )
        self.assertFalse(ok)
        self.assertIn("network", reason)


class ProvenanceTests(unittest.TestCase):
    def test_provenance_contains_license_and_schema(self):
        text = ih.render_provenance(
            source_path="/data/harbor/demo",
            schema_version="1.3",
            docker_required=True,
            docker_reasons=["RUN apt-get install -y gcc"],
            solution_notes=["copied 1 non-script solution file(s)"],
            attention=["needs-manual-solution"],
            collection="harbor-demo",
            import_date="2026-07-20",
        )
        self.assertIn("schema_version seen**: 1.3", text)
        self.assertIn("DOCKER-REQUIRED", text)
        self.assertIn("verify the upstream", text.lower())
        self.assertIn("harbor-demo", text)
        self.assertIn("2026-07-20", text)


class InstructionSafetyTests(unittest.TestCase):
    def test_rejects_logs_verifier(self):
        with self.assertRaises(ih.HarborImportError) as ctx:
            ih.check_instruction_safe("Write reward to /logs/verifier/reward.txt\n")
        self.assertIn("/logs/verifier", str(ctx.exception))

    def test_allows_clean_instruction(self):
        ih.check_instruction_safe("Fix the bug in main.py.\n")


class FixtureImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_import_fix_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_import_synthetic_harbor_sample(self):
        out = os.path.join(self.tmp, "tasks")
        results = ih.import_harbor_tasks(
            FIXTURE, out, collection="harbor-fixture", validate=True,
        )
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.ok, r.error)
        self.assertFalse(r.docker_required)
        self.assertTrue(r.solution_materialized)
        self.assertTrue(r.validated)
        task = r.out_dir
        self.assertTrue(os.path.isfile(os.path.join(task, "checker.sh")))
        self.assertTrue(
            os.path.isfile(
                os.path.join(task, "checker_data", "harbor-tests", "test.sh")
            )
        )
        self.assertTrue(os.path.isfile(os.path.join(task, "PROVENANCE.md")))
        self.assertTrue(
            os.path.isfile(os.path.join(task, "workspace", "greeting.txt"))
        )
        # Polarity via validate helpers.
        ws_code, ws_out, ws_raw = run_checker(task, False)
        sol_code, sol_out, sol_raw = run_checker(task, True)
        self.assertNotEqual(ws_code, 0, ws_out)
        self.assertEqual(sol_code, 0, sol_out)
        self.assertEqual(effective_score(sol_code, sol_raw), 1.0)

    def test_cli_import_harbor(self):
        out = os.path.join(self.tmp, "out")
        rc = ih.main([
            "harbor",
            "--from", FIXTURE,
            "--out", out,
            "--collection", "sample",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(
            os.path.isfile(os.path.join(out, "sample", "harbor-sample", "checker.sh"))
        )


@unittest.skipUnless(
    os.path.isdir(os.path.join(SOURCE_ROOT, "tasks", "make-it-run")),
    "core tasks/ not present in this install layout",
)
class RoundTripExportImportTests(unittest.TestCase):
    """Export a core task to Harbor, import it back, assert polarity matches."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_harbor_rt_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task_name = "make-it-run"
        self.original = os.path.join(SOURCE_ROOT, "tasks", self.task_name)

    def test_export_import_round_trip_polarity(self):
        harbor_out = os.path.join(self.tmp, "harbor")
        eh.export_tasks(
            os.path.join(SOURCE_ROOT, "tasks"),
            harbor_out,
            self.task_name,
        )
        exported = os.path.join(harbor_out, self.task_name)
        self.assertTrue(os.path.isfile(os.path.join(exported, "tests", "test.sh")))

        # Original polarity / scores.
        o_ws_code, o_ws_out, o_ws_raw = run_checker(self.original, False)
        o_sol_code, o_sol_out, o_sol_raw = run_checker(self.original, True)
        o_ws_score = effective_score(o_ws_code, o_ws_raw)
        o_sol_score = effective_score(o_sol_code, o_sol_raw)
        self.assertNotEqual(o_ws_code, 0)
        self.assertEqual(o_sol_code, 0)

        imported_root = os.path.join(self.tmp, "imported")
        results = ih.import_harbor_tasks(exported, imported_root, validate=True)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.solution_materialized)
        self.assertTrue(r.validated, r.notes)

        # Reimported checker verdicts match original on untouched + solution.
        i_ws_code, i_ws_out, i_ws_raw = run_checker(r.out_dir, False)
        i_sol_code, i_sol_out, i_sol_raw = run_checker(r.out_dir, True)
        i_ws_score = effective_score(i_ws_code, i_ws_raw)
        i_sol_score = effective_score(i_sol_code, i_sol_raw)

        self.assertEqual(
            (i_ws_code == 0), (o_ws_code == 0),
            f"untouched pass/fail mismatch\norig={o_ws_out!r}\nimp={i_ws_out!r}",
        )
        self.assertEqual(
            (i_sol_code == 0), (o_sol_code == 0),
            f"solution pass/fail mismatch\norig={o_sol_out!r}\nimp={i_sol_out!r}",
        )
        self.assertEqual(i_ws_score, o_ws_score)
        self.assertEqual(i_sol_score, o_sol_score)
        self.assertEqual(i_sol_score, 1.0)

    def test_cli_round_trip_smoke(self):
        harbor_out = os.path.join(self.tmp, "harbor_cli")
        self.assertEqual(
            eh.main([
                "harbor",
                "--task", self.task_name,
                "--tasks-dir", os.path.join(SOURCE_ROOT, "tasks"),
                "--out", harbor_out,
            ]),
            0,
        )
        imported = os.path.join(self.tmp, "imported_cli")
        rc = ih.main([
            "harbor",
            "--from", os.path.join(harbor_out, self.task_name),
            "--out", imported,
        ])
        self.assertEqual(rc, 0)


class DockerRequiredImportTests(unittest.TestCase):
    def test_run_dockerfile_still_imports_with_marker(self):
        tmp = tempfile.mkdtemp(prefix="obench_docker_req_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        harbor = os.path.join(tmp, "heavy")
        os.makedirs(os.path.join(harbor, "environment", "app"))
        os.makedirs(os.path.join(harbor, "tests"))
        os.makedirs(os.path.join(harbor, "solution"))
        _write(os.path.join(harbor, "instruction.md"), "Install nothing; just pass.\n")
        _write(
            os.path.join(harbor, "environment", "Dockerfile"),
            "FROM python:3.11-slim\n"
            "RUN apt-get update && apt-get install -y gcc\n"
            "WORKDIR /app\n"
            "COPY app/ /app/\n",
        )
        _write(os.path.join(harbor, "environment", "app", "x.txt"), "x\n")
        _write(
            os.path.join(harbor, "tests", "test.sh"),
            "#!/usr/bin/env bash\n"
            "REWARD_DIR=\"${VERIFIER_LOGS_DIR:-/logs/verifier}\"\n"
            "mkdir -p \"$REWARD_DIR\"\n"
            "echo 1.0 >\"$REWARD_DIR/reward.txt\"\n"
            "exit 0\n",
        )
        os.chmod(os.path.join(harbor, "tests", "test.sh"), 0o755)
        _write(os.path.join(harbor, "solution", "x.txt"), "x\n")
        _write(
            os.path.join(harbor, "task.toml"),
            'schema_version = "1.3"\n',
        )
        out = os.path.join(tmp, "out", "heavy")
        result = ih.import_task(harbor, out)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.docker_required)
        self.assertTrue(os.path.isfile(os.path.join(out, "REQUIREMENTS.md")))
        with open(os.path.join(out, "REQUIREMENTS.md"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("python:3.11-slim", body)
        self.assertIn("DOCKER-REQUIRED", body)


if __name__ == "__main__":
    unittest.main()
