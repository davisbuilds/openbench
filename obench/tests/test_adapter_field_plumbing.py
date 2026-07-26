#!/usr/bin/env python3
"""A field an adapter reports must actually reach the results row.

Adding one field requires registering it in THREE independent places:

  1. the adapter's returned dict          (obench/adapters/*.py)
  2. run.py's explicit result -> row copy (``row[x] = result.get(x)``)
  3. ROW_FIELDS, the serialization allowlist

Miss any one and the value is silently dropped -- no error, just ``None`` in the
data. All three failed in a single day:

  * ``host`` was added to the row literal but not ROW_FIELDS, so the next live
    cell still wrote ``host: None``;
  * ``model_context_window`` was stamped in ``run_routed`` instead of ``run``,
    the path that produces our data;
  * then stamped correctly in the adapter, but absent from run.py's copy block,
    so a docker cell recorded ``ctx=None`` while the container had the value.

Each was found by reading a real produced row, never by the test suite. This
test closes the loop: every field an adapter reports and the schema accepts must
be copied through.
"""

import ast
import os
import unittest

from obench import paths
from obench.run import ROW_FIELDS

# Reported by an adapter but deliberately not persisted per-field.
NOT_PERSISTED = {
    "full_output",      # local-only transcript; never published
    "output_tail",      # ditto, trimmed copy
    "cmd",              # rewritten by docker_exec into a nested dict
    "candidate_version",  # folded into harness_version
    "tokens",           # recomputed by the runner from the split
}


def _dict_literal_keys(path, func_names=None):
    """String keys of every dict literal returned inside the given functions."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    keys = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if func_names and fn.name not in func_names:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                keys |= {k.value for k in node.value.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return keys


def _copied_into_row(path):
    """Field names assigned as ``row["x"] = ...`` in run.py."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and getattr(target.value, "id", None) == "row"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                out.add(target.slice.value)
    return out


class AdapterFieldPlumbingTests(unittest.TestCase):
    def test_adapter_reported_fields_reach_the_row(self):
        adapter = os.path.join(paths.PACKAGE_DIR, "adapters", "pi.py")
        run_py = os.path.join(paths.PACKAGE_DIR, "run.py")
        reported = _dict_literal_keys(adapter)
        copied = _copied_into_row(run_py)
        dropped = sorted(
            f for f in reported
            if f in ROW_FIELDS and f not in copied and f not in NOT_PERSISTED)
        self.assertEqual(
            dropped, [],
            f"pi.py reports these and ROW_FIELDS accepts them, but run.py never "
            f"copies them into the row, so they persist as None: {dropped}")

    def test_the_two_limit_fields_specifically(self):
        # The concrete miss: a docker cell recorded ctx=None while the container
        # had 1048576, because this copy was absent.
        copied = _copied_into_row(os.path.join(paths.PACKAGE_DIR, "run.py"))
        for field in ("model_context_window", "model_max_tokens"):
            self.assertIn(field, copied, f"{field} is never copied into the row")
            self.assertIn(field, ROW_FIELDS, f"{field} is not in ROW_FIELDS")

    def test_the_guard_catches_a_dropped_field(self):
        # Negative control for the AST extraction itself.
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("def f():\n    row['a'] = 1\n    other['b'] = 2\n")
            self.assertEqual(_copied_into_row(path), {"a"})
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
