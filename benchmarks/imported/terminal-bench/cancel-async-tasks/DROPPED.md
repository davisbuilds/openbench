# DROPPED from the active task set — 2026-07-11

**Reason: load-sensitive checker.** A hash-verified correct solution scored
FAIL under host load and PASS when rerun idle, on three separate occasions:

1. Original checker (fixed `sleep 0.5` → SIGINT, `communicate(timeout=5)`):
   widespread wrong_answer rows across harnesses; extracted final-code bytes
   (sha256-matched against the checker_workspace_files manifest) re-passed
   the checker when rerun idle.
2. After the readiness-based rewrite (merged a2893c4: wait for "Task
   started." lines before SIGINT, 20s exit deadline): overnight batch runs
   still produced Terra 0/11 and Luna 0/9 while co-tenant workers loaded the
   host; the same failed bytes passed 8/8 when rerun idle.
3. The gate's synthetic-stress determinism check (20 runs, 6 CPU burners)
   passes it 20/20 — synthetic CPU load does not reproduce the real failure,
   so the flakiness cannot be certified away.

The static timing-sensitivity scan in `bench/admission_gate.py` flags this
checker's patterns (readiness-wait → signal, wall-clock exit deadlines) and
those patterns are now a hard admission failure.

**Status of historical data:** all cancel-async cells in `data/` are
quarantined — excluded from solve-rate denominators (see the QUARANTINE
sections in the per-run methodology notes). Headline stats use the remaining
sound tasks.

The task directory is retained for reproducibility of past runs only. Do not
schedule new cells on it.
