#!/usr/bin/env python3
"""Backfill replies_ok/replies_throttled into rows from saved transcripts.

Rows written before the adapter reported reply health cannot show a 429 storm:
a storm cell completes, produces some tokens, and receives a real checker
verdict, so ``failure_class=wrong_answer, error=None, turns=16`` is all the row
says. The signal lives only in the transcript (assistant messages with
``stopReason=error`` and a 429 errorMessage).

This reads each cell's saved pi JSON transcript, counts delivered vs
429-killed assistant replies, and writes a NEW results file with the two fields
added -- never mutating the input. ``class_for_report`` then reclassifies
throttle-dominated cells as rate_limited on read, so coverage warnings and
solve rates become honest in every existing tool.

Usage:
    python3 -m obench.backfill_reply_health RESULTS.jsonl TRANSCRIPTS_DIR --out OUT.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def reply_health_from_transcript(path):
    """(replies_ok, replies_throttled) from a pi JSON-stream transcript."""
    ok = throttled = 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "message_end":
                continue
            msg = event.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            if msg.get("stopReason") == "error":
                err = str(msg.get("errorMessage") or "")
                if "429" in err or "rate limit" in err.lower() or "rate-limit" in err.lower():
                    throttled += 1
            else:
                ok += 1
    return ok, throttled


def transcript_path(transcripts_dir, row):
    harness = str(row.get("harness", "")).split("@")[0]
    task = str(row.get("task", "")).replace("/", "_")
    name = f"{harness}_{task}_{row.get('model')}_trial{row.get('trial')}.txt"
    return os.path.join(transcripts_dir, name)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="obench backfill-reply-health",
                                 description=__doc__)
    ap.add_argument("results")
    ap.add_argument("transcripts_dir")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    filled = missing = already = 0
    with open(args.results, encoding="utf-8") as src, \
            open(args.out, "w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row.get("replies_ok"), int):
                already += 1
            else:
                health = reply_health_from_transcript(
                    transcript_path(args.transcripts_dir, row))
                if health is None:
                    missing += 1
                else:
                    row["replies_ok"], row["replies_throttled"] = health
                    filled += 1
            dst.write(json.dumps(row) + "\n")
    print(f"backfilled {filled} rows ({already} already had fields, "
          f"{missing} had no transcript) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
