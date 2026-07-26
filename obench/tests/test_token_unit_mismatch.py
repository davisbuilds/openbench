#!/usr/bin/env python3
"""A cumulative token total must never be compared to a per-request limit.

``tokens_output`` is the SUM of output tokens over every turn in a cell.
``max_completion_tokens`` / ``max_tokens`` bound a SINGLE reply. The names line
up, the units do not, so ``tokens_output >= cap`` reports any multi-turn run as
truncated. Verified on a real cell: turns=18, tokens_output=28466, which is
exactly the sum of its 18 per-call outputs.

This was published twice in one session:

    "224 scored failures truncated at the cap"   -> real answer 11
    "56% of laguna / 65% of deepseek cells"      -> real answer 47% and 0%

The second came after the first had already been caught and corrected. A third
variant followed: the fix itself assumed "reply at cap == truncated" without
checking the cap was enforced, and deepseek replies turned out to EXCEED their
own 8192 cap by up to 4.9x -- so that signal is meaningless on that route.

Three preventions here:
  * ``has_per_reply_truncation`` owns the correct arithmetic (exact per-call
    values from ``usage_raw``), so no analysis needs to hand-roll it;
  * an AST guard fails if any obench module compares a cumulative token field
    to a per-request cap again;
  * the helper refuses to claim truncation on a cell whose replies exceed the
    cap, since that proves the cap is not binding there.
"""

import ast
import os
import unittest

from obench import failure_class as fc
from obench import paths

CUMULATIVE_FIELDS = {"tokens_output", "tokens", "tokens_input_uncached",
                     "tokens_fresh", "tokens_proxy_output", "tokens_reasoning"}
PER_REQUEST_CAPS = {"max_completion_tokens", "max_tokens"}


def _row(replies, cap, **kw):
    row = {"harness": "pi", "completed": True, "checker_exit": 1,
           "turns": len(replies), "tokens_output": sum(replies),
           "usage_raw": [{"output": r} for r in replies],
           "sampling_observed": [{"max_completion_tokens": cap}]}
    row.update(kw)
    return row


class PerReplyTruncationTests(unittest.TestCase):
    def test_multi_turn_run_over_the_cap_in_total_is_NOT_truncated(self):
        # THE false positive that was published twice: 5 replies of 7000 sum to
        # 35000 > 32768, but no single reply came close to the cap.
        row = _row([7000] * 5, 32768)
        self.assertGreater(row["tokens_output"], 32768)
        self.assertFalse(fc.has_per_reply_truncation(row))

    def test_single_reply_at_the_cap_is_truncated(self):
        self.assertTrue(fc.has_per_reply_truncation(_row([32768], 32768)))

    def test_one_capped_reply_among_many_is_truncated(self):
        self.assertTrue(fc.has_per_reply_truncation(_row([120, 32768, 90], 32768)))

    def test_no_per_call_data_means_no_claim(self):
        row = _row([100], 32768)
        row["usage_raw"] = None
        self.assertFalse(fc.has_per_reply_truncation(row))

    def test_no_observed_cap_means_no_claim(self):
        row = _row([100], 32768)
        row["sampling_observed"] = []
        self.assertFalse(fc.has_per_reply_truncation(row))

    def test_per_reply_outputs_are_exact_not_averaged(self):
        self.assertEqual(fc.per_reply_outputs(_row([1, 2, 3], 100)), (1, 2, 3))


class NoUnitMismatchInSourceTests(unittest.TestCase):
    """Fail if any module compares a cumulative total to a per-request cap."""

    def _offenders(self, path):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        found = []

        def names(node):
            out = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.add(sub.value)
                elif isinstance(sub, ast.Attribute):
                    out.add(sub.attr)
                elif isinstance(sub, ast.Name):
                    out.add(sub.id)
            return out

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = names(node.left)
            right = set().union(*(names(c) for c in node.comparators)) if node.comparators else set()
            if (left & CUMULATIVE_FIELDS and right & PER_REQUEST_CAPS) or \
               (right & CUMULATIVE_FIELDS and left & PER_REQUEST_CAPS):
                found.append(f"{os.path.basename(path)}:{node.lineno}")
        return found

    def test_no_module_compares_cumulative_tokens_to_a_per_request_cap(self):
        offenders = []
        for name in sorted(os.listdir(paths.PACKAGE_DIR)):
            if not name.endswith(".py"):
                continue
            offenders += self._offenders(os.path.join(paths.PACKAGE_DIR, name))
        self.assertEqual(
            offenders, [],
            f"cumulative token total compared to a per-request cap at "
            f"{offenders}. tokens_output is a SUM over turns; use "
            f"failure_class.has_per_reply_truncation() instead.")

    def test_the_guard_actually_catches_the_bug(self):
        # Negative control for the AST check itself, so it cannot silently
        # degrade into a test that passes on everything.
        import tempfile
        bad = ("def f(row, cap):\n"
               "    return row['tokens_output'] >= row['max_completion_tokens']\n")
        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(bad)
            self.assertTrue(self._offenders(path),
                            "the AST guard failed to flag a known unit mismatch")
        finally:
            os.unlink(path)


class CapEnforcementTests(unittest.TestCase):
    """No truncation claim where the cap is demonstrably not enforced."""

    def test_reply_exceeding_its_cap_yields_no_claim(self):
        # deepseek: a 17096-token reply against an 8192 cap. The cap is not
        # binding on that route, so a reply "at the cap" means nothing.
        self.assertFalse(fc.has_per_reply_truncation(_row([500, 17096], 8192)))

    def test_enforced_cap_still_detects_truncation(self):
        self.assertTrue(fc.has_per_reply_truncation(_row([500, 32768], 32768)))

    def test_the_check_is_per_cell_not_global(self):
        # One model overshooting must not suppress detection for another cell.
        self.assertFalse(fc.has_per_reply_truncation(_row([9000], 8192)))
        self.assertTrue(fc.has_per_reply_truncation(_row([8192], 8192)))


if __name__ == "__main__":
    unittest.main()
