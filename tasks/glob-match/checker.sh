#!/usr/bin/env bash
# Graded checker for glob-match.
#
# cwd is a fresh copy of the task workspace, which should now contain the
# agent's glob_match.py. Cases live in $TASK_DIR/checker_data/cases.tsv, one per
# line: <pattern>\t<path>\t<expected true|false>. Paths may be empty, so the TSV
# is parsed in python (bash `read` with a tab IFS collapses empty fields).
#
# GRADED: count how many cases the agent's program gets right, emit
# `SCORE: passed/total`, and exit 0 IFF all pass (nonzero otherwise carries the
# partial score as credit).
set -uo pipefail

if [ ! -f glob_match.py ]; then
    echo "FAIL: glob_match.py not found in workspace" >&2
    echo "SCORE: 0.0"
    exit 1
fi

python3 - "$TASK_DIR/checker_data/cases.tsv" <<'PY'
import subprocess
import sys

cases_path = sys.argv[1]
try:
    with open(cases_path, encoding="utf-8") as fh:
        rows = [ln.rstrip("\n") for ln in fh if ln.strip("\n") != ""]
except FileNotFoundError:
    print("FAIL: cases file not found: %s" % cases_path, file=sys.stderr)
    print("SCORE: 0.0")
    sys.exit(1)

total = passed = 0
for ln in rows:
    parts = ln.split("\t")
    if len(parts) != 3:
        continue
    pat, path, want = parts
    total += 1
    try:
        out = subprocess.run(
            ["python3", "glob_match.py", pat, path],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:  # noqa: BLE001 - any runner failure is a miss
        out = "<error:%s>" % e
    if out == want:
        passed += 1
    else:
        print("FAIL: pattern=[%s] path=[%s] want=%s got=[%s]" % (pat, path, want, out),
              file=sys.stderr)

if total == 0:
    print("FAIL: no cases parsed", file=sys.stderr)
    print("SCORE: 0.0")
    sys.exit(1)

print("SCORE: %.4f" % (passed / total))
print("passed %d/%d glob cases" % (passed, total))
sys.exit(0 if passed == total else 1)
PY
