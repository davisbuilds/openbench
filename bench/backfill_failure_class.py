#!/usr/bin/env python3
"""Backfill failure_class into an existing results JSONL without mutating it.

Usage:
    python3 bench/backfill_failure_class.py IN.jsonl OUT.jsonl [--summary]

The runner's write-time classifier can scan adapter ``full_output`` before it is
trimmed. Historical rows normally contain only saved fields (notably
``output_tail`` when present), so backfill uses row fields + ``output_tail`` and
can miss markers truncated out of the tail.
"""

import argparse
import json
import os
from collections import Counter

from failure_class import FAILURE_CLASSES, classify_failure


def iter_rows(path):
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc


def _same_output_target(in_path, out_path):
    """Return True when output would alias input, including symlinks/hardlinks."""
    if os.path.abspath(in_path) == os.path.abspath(out_path):
        return True
    try:
        return os.path.exists(out_path) and os.path.samefile(in_path, out_path)
    except OSError:
        return False


def backfill(in_path, out_path):
    """Write a new JSONL with failure_class added; return class counts."""
    if _same_output_target(in_path, out_path):
        raise SystemExit("refusing to mutate results in place; choose a different output path")
    counts = Counter()
    rows = 0
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        for _line_no, row in iter_rows(in_path):
            fc = row.get("failure_class")
            if fc not in FAILURE_CLASSES:
                fc = classify_failure(row, row.get("output_tail") or "")
            row["failure_class"] = fc
            counts[fc] += 1
            rows += 1
            out.write(json.dumps(row) + "\n")
    return rows, counts


def format_summary(rows, counts, out_path):
    parts = [f"rows={rows}"] + [f"{fc}={counts.get(fc, 0)}" for fc in FAILURE_CLASSES]
    parts.append(f"output={out_path}")
    parts.append("note=backfill uses saved row fields + output_tail only; tail-only detection is weaker than write-time full_output scanning")
    return "\n".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill failure_class into a new results JSONL")
    parser.add_argument("input_jsonl")
    parser.add_argument("output_jsonl")
    parser.add_argument("--summary", action="store_true", help="print class counts")
    args = parser.parse_args(argv)

    rows, counts = backfill(args.input_jsonl, args.output_jsonl)
    if args.summary:
        print(format_summary(rows, counts, args.output_jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
