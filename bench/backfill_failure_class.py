#!/usr/bin/env python3
"""Backfill failure_class into an existing results JSONL without mutating it.

Usage:
    python3 bench/backfill_failure_class.py IN.jsonl OUT.jsonl [--summary] [--reclassify]

The runner's write-time classifier can scan adapter ``full_output`` before it is
trimmed. Historical rows normally contain only saved fields (notably
``output_tail`` when present), so backfill uses row fields + ``output_tail`` and
can miss markers truncated out of the tail. Backfill still applies cap-riding
empty-output detection when the timeout cap is saved in the row or recoverable
from timeout boilerplate such as ``timeout after 1200s``.
"""

import argparse
import json
import os
import re
from collections import Counter

from failure_class import FAILURE_CLASSES, classify_failure

_TIMEOUT_AFTER_RE = re.compile(r"\btimeout after (\d+(?:\.\d+)?)s\b", re.IGNORECASE)


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


def _timeout_s_for_row(row):
    """Return a timeout cap saved on, or inferable from, a historical row."""
    if row.get("timeout_s") is not None:
        return row.get("timeout_s")
    for field in ("error", "output_tail"):
        match = _TIMEOUT_AFTER_RE.search(str(row.get(field) or ""))
        if match:
            return float(match.group(1))
    return None


def backfill(in_path, out_path, reclassify=False):
    """Write a new JSONL with failure_class added; return class counts.

    By default this preserves existing valid classes and only fills missing or
    invalid values. Pass ``reclassify=True`` to revisit every row under the
    current taxonomy. Existing ``rate_limited`` rows remain rate-limited when
    tail-only recomputation lacks the write-time provider marker.
    """
    if _same_output_target(in_path, out_path):
        raise SystemExit("refusing to mutate results in place; choose a different output path")
    counts = Counter()
    rows = 0
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out:
        for _line_no, row in iter_rows(in_path):
            fc = row.get("failure_class")
            if reclassify or fc not in FAILURE_CLASSES:
                old_fc = fc
                fc = classify_failure(row, row.get("output_tail") or "", _timeout_s_for_row(row))
                # Historical backfill is tail-only. Preserve an existing
                # rate_limited class when recomputation finds no saved marker;
                # otherwise --reclassify would erase write-time full-output
                # provider detections that were intentionally not persisted.
                if reclassify and old_fc == "rate_limited" and fc == "wrong_answer":
                    fc = old_fc
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
    parser.add_argument("--reclassify", action="store_true",
                        help="revisit every row instead of only filling missing/invalid values")
    args = parser.parse_args(argv)

    rows, counts = backfill(args.input_jsonl, args.output_jsonl, reclassify=args.reclassify)
    if args.summary:
        print(format_summary(rows, counts, args.output_jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
