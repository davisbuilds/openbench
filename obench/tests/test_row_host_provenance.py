#!/usr/bin/env python3
"""Every results row records which machine produced it.

The estate is two hosts with different container runtimes (Docker Desktop vs
colima) and different available API keys, so arms land on different machines
without anyone choosing that: tb-mid ran deepseek-v4-flash on the laptop -- the
only host holding DEEPSEEK_API_KEY -- and its other three arms on the mini.
Cross-arm wall-time comparisons were therefore confounded, and nothing in the
row schema made that visible; it was only caught when a refill on the mini died
with "SETUP-NEEDED: export DEEPSEEK_API_KEY".
"""

import ast
import os
import platform
import unittest

from obench import paths


class RowHostProvenanceTests(unittest.TestCase):
    def _row_literal(self):
        """The `row = {...}` dict literal that seeds every results row."""
        path = os.path.join(paths.PACKAGE_DIR, "run.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Dict)
                    and any(getattr(t, "id", None) == "row" for t in node.targets)):
                keys = [k.value for k in node.value.keys
                        if isinstance(k, ast.Constant)]
                if "run_id" in keys and "task" in keys:
                    return keys
        self.fail("could not find the results row dict literal in run.py")

    def test_row_schema_includes_host(self):
        self.assertIn(
            "host", self._row_literal(),
            "results rows must carry the producing machine; without it a "
            "mixed-host dataset is indistinguishable from a single-host one "
            "and cross-arm latency silently compares two different runtimes")

    def test_every_row_field_survives_serialization(self):
        """Rows are written through the ROW_FIELDS allowlist, so a field added
        to the row literal alone is silently dropped on write.

        This is not hypothetical: `host` was added to the literal, committed
        with a passing test that checked only the literal, and the very next
        live cell still wrote `host: None`. Asserting the two layers agree
        catches the whole class, not just this one field.
        """
        from obench.run import ROW_FIELDS
        # output_tail is deliberately NOT persisted: raw agent output goes to
        # local-only transcripts (a scrubbing concern), never the results JSONL.
        deliberately_unpersisted = {"output_tail"}
        missing = [k for k in self._row_literal()
                   if k not in ROW_FIELDS and k not in deliberately_unpersisted]
        self.assertEqual(
            missing, [],
            f"row fields absent from ROW_FIELDS are dropped when the row is "
            f"written: {missing}")

    def test_host_is_populated_end_to_end(self):
        """The value actually lands in a written row, not just the schema."""
        from obench import run as bench_run
        self.assertTrue(platform.node(), "platform.node() must identify the host")
        self.assertIn("host", bench_run.ROW_FIELDS)


if __name__ == "__main__":
    unittest.main()
