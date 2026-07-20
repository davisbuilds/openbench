#!/usr/bin/env python3
"""Tests for bench/tools/parity_backfill.py using real multi-turn transcript cuts."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from obench.tools import parity_backfill

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "parity_backfill"
RESULTS = FIXTURE_DIR / "results.jsonl"
TRANSCRIPTS = FIXTURE_DIR


class ParityBackfillTests(unittest.TestCase):
    def test_dry_run_uses_real_multiturn_fixtures_and_self_tests(self):
        summary = parity_backfill.backfill_file(RESULTS, TRANSCRIPTS, write=False)
        self.assertEqual(summary["rows"], 5)
        self.assertEqual(summary["pi_exact_match_self_test"], {"mismatches": 0})

        lanes = summary["lanes"]
        for harness in ("pi", "opencode", "claude", "codex"):
            self.assertEqual(lanes[harness]["rows"], 1)
            self.assertEqual(lanes[harness]["unavailable"], 0)
            self.assertEqual(lanes[harness]["basis"], {"vendor_split": 1})
            self.assertGreater(lanes[harness]["tokens_fresh_sum"], 0)

        # The fixtures are cut from actual transcripts and remain multi-turn / multi-step.
        self.assertGreaterEqual(
            self._count_json_events("pi_terminal-bench_cancel-async-tasks_deepseek-v4-flash_trial1.txt", "turn_end"),
            2,
        )
        self.assertGreaterEqual(
            self._count_json_events("opencode_terminal-bench_cancel-async-tasks_deepseek-v4-flash_trial1.txt", "step_finish"),
            2,
        )
        self.assertGreater(self._claude_num_turns(), 1)
        self.assertGreaterEqual(
            self._count_json_events("codex_terminal-bench_cancel-async-tasks_deepseek-v4-flash_trial1.txt", "turn.completed"),
            2,
        )

        self.assertEqual(lanes["pi"]["old_sum"], lanes["pi"]["tokens_fresh_sum"])
        self.assertEqual(lanes["claude"]["claude_reconcile_exact"], 1)
        self.assertEqual(lanes["claude"]["claude_reconcile_rows"], 1)
        self.assertEqual(lanes["grokbuild"]["tokens_fresh_sum"], 123)

    def test_write_is_idempotent_and_creates_single_backup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result_path = tmp / "tb-open-n3-deepseek-v4-flash.jsonl"
            shutil.copy2(RESULTS, result_path)
            shutil.copytree(FIXTURE_DIR / "tb-open-n3-deepseek-v4-flash", tmp / "tb-open-n3-deepseek-v4-flash")

            first = parity_backfill.backfill_file(result_path, tmp, write=True)
            backup = result_path.with_name(result_path.name + ".pre-parity.bak")
            self.assertTrue(backup.exists())
            self.assertTrue(first["backup_created"])
            self.assertEqual(backup.read_text(), RESULTS.read_text())

            rows_after_first = result_path.read_text()
            second = parity_backfill.backfill_file(result_path, tmp, write=True)
            self.assertFalse(second["backup_created"])
            self.assertEqual(result_path.read_text(), rows_after_first)
            self.assertEqual(backup.read_text(), RESULTS.read_text())

            rows = [json.loads(line) for line in result_path.read_text().splitlines()]
            pi = next(row for row in rows if row["harness"] == "pi")
            self.assertEqual(pi["tokens_fresh"], pi["tokens"])
            self.assertEqual(pi["token_basis"], "vendor_split")
            self.assertIn("tokens_input_uncached", pi)
            self.assertIn("tokens_cache_read", pi)

    def test_missing_transcript_adopts_scalar_only_for_exact_basis_lanes(self):
        # pi's legacy scalar IS the target basis (probe-verified), so a pi row
        # with no transcript adopts it as tokens_fresh. claude's scalar uses a
        # different basis (includes cache writes) and must stay unavailable.
        with tempfile.TemporaryDirectory() as td:
            result_path = Path(td) / "results.jsonl"
            mk = lambda h: json.dumps({
                "run_id": f"{h}:terminal-bench/missing:deepseek-v4-flash:trial1",
                "harness": h,
                "model": "deepseek-v4-flash",
                "task": "terminal-bench/missing",
                "trial": 1,
                "tokens": 99,
            })
            result_path.write_text(mk("pi") + "\n" + mk("claude") + "\n")
            summary = parity_backfill.backfill_file(result_path, Path(td), write=True)
            self.assertEqual(summary["lanes"]["pi"]["scalar_adopted"], 1)
            self.assertEqual(summary["lanes"]["pi"]["unavailable"], 0)
            self.assertEqual(summary["lanes"]["claude"]["unavailable"], 1)
            pi_row, claude_row = [json.loads(l) for l in result_path.read_text().splitlines()]
            self.assertEqual(pi_row["token_basis"], "scalar_exact")
            self.assertEqual(pi_row["tokens_fresh"], 99)
            for field in parity_backfill.TOKEN_FIELDS:
                self.assertIsNone(pi_row[field])
            self.assertEqual(claude_row["token_basis"], "unavailable")
            self.assertIsNone(claude_row["tokens_fresh"])

    def test_scalar_adoption_is_idempotent_across_passes(self):
        # A grokbuild row with only a legacy scalar must keep tokens_fresh
        # through repeated backfill passes (regression: pass 2 wiped the
        # adopted value while leaving token_basis=scalar_exact).
        with tempfile.TemporaryDirectory() as td:
            result_path = Path(td) / "results.jsonl"
            result_path.write_text(json.dumps({
                "run_id": "grokbuild:terminal-bench/x:deepseek-v4-flash:trial1",
                "harness": "grokbuild",
                "model": "deepseek-v4-flash",
                "task": "terminal-bench/x",
                "trial": 1,
                "tokens": 8982,
            }) + "\n")
            for _ in range(3):
                parity_backfill.backfill_file(result_path, Path(td), write=True)
                row = json.loads(result_path.read_text())
                self.assertEqual(row["tokens_fresh"], 8982)
                self.assertEqual(row["token_basis"], "scalar_exact")

    def test_missing_transcript_pi_without_scalar_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            result_path = Path(td) / "results.jsonl"
            result_path.write_text(json.dumps({
                "run_id": "pi:terminal-bench/missing:deepseek-v4-flash:trial1",
                "harness": "pi",
                "model": "deepseek-v4-flash",
                "task": "terminal-bench/missing",
                "trial": 1,
                "tokens": None,
            }) + "\n")
            summary = parity_backfill.backfill_file(result_path, Path(td), write=True)
            self.assertEqual(summary["lanes"]["pi"]["unavailable"], 1)
            row = json.loads(result_path.read_text())
            self.assertEqual(row["token_basis"], "unavailable")
            self.assertIsNone(row["tokens_fresh"])

    def test_pi_mismatch_is_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result_path = tmp / "tb-open-n3-deepseek-v4-flash.jsonl"
            rows = [json.loads(line) for line in RESULTS.read_text().splitlines()]
            rows = [row for row in rows if row["harness"] == "pi"]
            rows[0]["tokens"] += 1
            result_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            shutil.copytree(FIXTURE_DIR / "tb-open-n3-deepseek-v4-flash", tmp / "tb-open-n3-deepseek-v4-flash")
            with self.assertRaises(parity_backfill.BackfillError):
                parity_backfill.backfill_file(result_path, tmp, write=True)
            self.assertFalse(result_path.with_name(result_path.name + ".pre-parity.bak").exists())

    def _count_json_events(self, filename, event_type):
        count = 0
        with (FIXTURE_DIR / "tb-open-n3-deepseek-v4-flash" / filename).open(encoding="utf-8") as fh:
            for raw in fh:
                obj = json.loads(raw)
                if obj.get("type") == event_type:
                    count += 1
        return count

    def _claude_num_turns(self):
        path = FIXTURE_DIR / "tb-open-n3-deepseek-v4-flash" / "claude_terminal-bench_cancel-async-tasks_deepseek-v4-flash_trial1.txt"
        return json.loads(path.read_text())["num_turns"]


if __name__ == "__main__":
    unittest.main()
