#!/usr/bin/env bash
# Fixture checker: succeeds (exit 0) iff done.txt in cwd contains exactly SOLVED.
# Runs with cwd=<disposable workspace copy> and TASK_DIR=<abs fixture task dir>.
set -u
if [ ! -f done.txt ]; then
  echo "done.txt missing" >&2
  exit 1
fi
content="$(cat done.txt)"
if [ "$content" = "SOLVED" ]; then
  exit 0
fi
echo "done.txt content mismatch: '$content'" >&2
exit 1
