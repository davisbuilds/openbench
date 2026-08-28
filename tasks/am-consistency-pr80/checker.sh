#!/usr/bin/env bash
# Graded checker for am-consistency-pr80.
#
# Two independent, real cross-site-consistency defects (from agentmonitor PR #80
# Codex P1/P2 findings) live somewhere in the src/ tree. Each has a dedicated
# hidden regression test, kept OUT of the agent workspace so the agent must
# locate the defect by reading the codebase, not by reading the test. SCORE =
# fraction of the two findings the agent actually fixed (0, 0.5, or 1.0).
#
# The agent workspace ships without node_modules (the agent sandbox has no
# network). This checker runs unsandboxed on the host, so it provisions deps by
# symlinking a canonical agentmonitor node_modules read-only, drops the hidden
# tests into tests/, and runs each finding's target test by name.
set -uo pipefail

# Canonical deps (native better-sqlite3 built for this host's node). Override via
# AGENTMONITOR_DEPS. Machine-specific by design -- this is a fork-local task.
DEPS="${AGENTMONITOR_DEPS:-/Users/dg-mac-mini/Dev/agentmonitor/node_modules}"

fail0() { echo "SCORE: 0.0"; exit 1; }

if [ ! -d src ]; then echo "FAIL: src/ missing in workspace" >&2; fail0; fi
if [ ! -d "$DEPS" ]; then
  echo "FAIL: node_modules not found at $DEPS (set AGENTMONITOR_DEPS)" >&2; fail0
fi

# provision deps (read-only symlink) + hidden regression tests
[ -e node_modules ] || ln -s "$DEPS" node_modules
mkdir -p tests
cp "$TASK_DIR/checker_data/skill-context-parser.test.ts" tests/ || fail0
cp "$TASK_DIR/checker_data/skill-consultation-analytics.test.ts" tests/ || fail0

# run one named test; echo PASS iff >=1 matched and 0 failed
run_one() { # <file> <test-name-regex>
  node --import tsx --test --test-reporter=tap \
    --test-name-pattern="$2" "tests/$1" 2>/dev/null \
    | awk '/^# pass /{p=$3} /^# fail /{f=$3}
           END { if (p+0>=1 && f+0==0) print "PASS"; else print "FAIL" }'
}

f2=$(run_one skill-context-parser.test.ts \
     'Codex preserves catalog bytes across contiguous content fragments')
f1=$(run_one skill-consultation-analytics.test.ts \
     'classifies in-window consultations against earlier history in the same session')

passed=0
if [ "$f2" = PASS ]; then passed=$((passed + 1));
  else echo "MISS F2: Codex content-fragment byte preservation still broken" >&2; fi
if [ "$f1" = PASS ]; then passed=$((passed + 1));
  else echo "MISS F1: in-window consultation classification still broken" >&2; fi

python3 -c "print('SCORE: %.4f' % ($passed / 2))"
echo "fixed $passed/2 findings"
[ "$passed" -eq 2 ] && exit 0 || exit 1
