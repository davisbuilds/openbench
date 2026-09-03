# Fix three telemetry defects in the agentmonitor codebase

This workspace is the `agentmonitor` codebase (TypeScript, `src/`). It ingests
coding-agent session logs and reports analytics about how skills are consulted.
Three defects have been reported from production. Each is described **only by its
observable symptom** below — diagnose the root cause yourself by reading the code,
then fix it in `src/`. Do **not** modify or add tests; change only the
implementation under `src/`.

The three are independent; fixing any subset earns partial credit.

## Symptom 1 — some Codex skill payloads have the wrong size and occasionally go undetected

For a subset of imported Codex sessions, the recorded byte size and fingerprint
of a captured skill payload do not match the payload actually present in the
session log, and once in a while a skills catalog that is clearly present in the
log is reported as absent. Sessions where the relevant payload happens to be
recorded in a single piece are unaffected; the discrepancy only shows up for some
sessions and not others. The stored size/fingerprint should exactly reflect the
payload as it appears at runtime.

## Symptom 2 — first-read engagement is overstated for date-filtered views

When the skill-consultation dashboard is filtered to a date range, the
`first_read` share for some skills is inflated and the class breakdown
(`first_read` vs `rehydration_after_compaction` vs `repeat_no_compaction`) is
wrong. It only happens for a skill that was already consulted earlier in the same
session, before the start of the selected range; a consultation inside the range
that is really a repeat gets counted as a brand-new first read. The unfiltered
(all-time) view reports the same skills correctly. Class decomposition for a
date-filtered view should match what you would get by classifying against the
whole session and only then restricting to the window.

## Symptom 3 — currently-active sessions are dropped from window counts

Some sessions that are still active are missing from the "sessions in window"
counts (and the eligible denominator) for a selected date range, which throws off
the engagement rates. It affects active sessions whose most recent parsed activity
happens to predate the start of the window — for example a session that has been
running since before midnight and is still live during the window. Such an active
session overlaps the window through the present and should be counted.

## Success

All three symptoms resolved, with the observable behavior corrected. Partial
credit is given per symptom fixed.
