# Repair inconsistent benchmark study accounting

This is the backend of a local telemetry service. It imports JSONL benchmark
results, keeps ordinary activity separate, and summarizes each study into model
arms. Investigate and repair three reported problems while preserving existing
import, usage, and aggregation contracts.

## Study import and replay

Repeating a bake-off can make the new study disappear even though the input
carries a different study identity. A cell's run identifier is unique within one
study; different studies may legitimately reuse it. Importing one study again
must remain idempotent, retain the original run identifier in metadata, and
backfill a previously unknown cost only for that study's cell. Never replace an
already known cost with a later import's value.

Older files have no explicit study fields and belong to their containing
directory. Importing the same file through an absolute, bare-filename, or
parent-relative path must produce the same study. Files in sibling containing
directories with different directory names must remain separate even when their
filenames and cell identifiers match. The internal study identifier may use a
directory name or a normalized directory path; only stable grouping is required. Keep explicit study fields and the manual study override authoritative.

## Upgrading existing installations

Some older installations have a restrictive event-type table definition. After
upgrade, study identity must remain available, previously assigned study metadata
and event values must survive, and newly supported event types must work. A
second initialization must preserve the upgraded data. Exercise upgrades through
the existing public `initSchema()` startup entry point; the migration mechanism
and migration marker are implementation choices.

There are also pre-release benchmark rows without study identity, created before
the current import format. These may be discarded during a one-time upgrade and
reconstructed by replaying their source files. After that replay, benchmark
usage must count each cell exactly once. Preserve already identified benchmark
rows and all ordinary activity, and keep subsequent startups and reimports
idempotent.

## Incomplete study coverage

The study detail can claim no excluded trials when an arm is missing cells or
contains unscored failed attempts. Each arm is expected to cover the study's full
combination of observed tasks and trial labels. Count missing or unscored cells
as exclusions; average scores only over scored observations, including valid
zero scores. A complete arm should report no exclusions. Keep costs and the
separation between benchmark and ordinary usage intact. Coverage is assessed
through each arm's `excluded_trials`, `n`, and `mean_score`; this task does not
require changing the meaning of the separate `expected_trials` summary field.
Storage-level event and session identifiers may be chosen freely as long as
the stated import, metadata, cost and aggregation behavior is preserved.

Change implementation under `src/`. The verifier supplies its own tests and
isolated database. Each of the three areas earns one-third credit only when all
of its behavioral checks pass; regressions in ordinary usage segregation or
manual identity handling invalidate the score.
