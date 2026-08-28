# Fix two consistency defects in the agentmonitor telemetry code

This workspace is the `agentmonitor` codebase (TypeScript, `src/`). It records
and analyzes coding-agent telemetry. Two real defects have been reported against
the skill-consultation telemetry. Both are **consistency bugs**: a value is
computed one way in one place and a different, inconsistent way in the place that
must agree with it. Find each defect in `src/` and fix it. Do **not** modify or
add tests; change only the implementation under `src/`.

## Defect 1 — Codex content fragments are not preserved byte-for-byte

When a Codex session log stores a single logical content block (for example a
`<skills_instructions>` payload) split across several *contiguous* content
fragments, the parser that reassembles those fragments inserts separator bytes
that were never in the original payload. Because downstream code takes an exact
byte measurement and fingerprint of that reassembled text — and parses tags out
of it — the inserted bytes corrupt the measurement and can make a tag or catalog
entry fail to parse at all. The reassembled content must equal the concatenation
of the retained fragments with no bytes added between them.

## Defect 2 — windowed consultation stats misclassify repeats as first reads

The skill-consultation analytics classify each in-window consultation of a skill
as `first_read`, `rehydration_after_compaction`, or `repeat_no_compaction`. The
classification of an occurrence is supposed to consider that skill's **full
history within the session**, then the requested date window is applied only when
aggregating the reported numbers. Currently, when a date window is requested, a
consultation that occurs inside the window but whose earlier consultation of the
same skill happened *before* the window (earlier in the same session) is treated
as having no prior generation and is reported as `first_read` — inflating
first-read engagement and corrupting the class breakdown. An in-window repeat
whose prior consultation is outside the window must still be classified against
that earlier history (e.g. as `rehydration_after_compaction`), not as `first_read`.

## Success

Both defects fixed, with the observable behavior corrected. Partial credit is
given for fixing one of the two.
