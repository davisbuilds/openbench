# AgentMonitor benchmark accounting — PR #106

## Source and license

- Public repository: https://github.com/davisbuilds/agentmonitor
- Reviewed PR: https://github.com/davisbuilds/agentmonitor/pull/106
- Snapshot base and complete oracle: `d33ef01584fe78f686ef921995f55e0ce70b9760`.
- Pre-review defective backend: `4cfbddaa788dddb205d19225135cab44578f5fbe`.
- Intermediate repair: `af2976f4ccc9f96140422fe7c3986b45b5bb0089`.
- The original package declares ISC and author `davisbuilds`; its package and lock
  metadata are retained verbatim. That commit has no standalone LICENSE file;
  this artifact does not invent an upstream license text.

The source tree is the complete backend `src/` from the oracle commit. Four files
are restored verbatim to their pre-review versions:

- `src/import/benchmark.ts`
- `src/db/schema.ts`
- `src/db/queries.ts`
- `src/db/v2-queries.ts`

The solution overlay restores exactly those files to the oracle commit. This is
an explicitly constructed mixed snapshot, not an untouched historical checkout.
All other source bytes are unmodified. `SOURCE_MANIFEST.json` records per-file
SHA256s and source revisions. Package metadata, lockfile, workspace configuration,
TypeScript configuration, and `.nvmrc` come from the oracle commit.

The model workspace excludes Git history, review comments, provenance, solution,
verifier, upstream tests, frontend, documentation, credentials, and live data.
The instruction describes observable contracts without naming implementation
files. Hidden checks are outside the workspace and injected only into disposable
verifier copies.

## Behavioral scoring

Three equally weighted buckets require all their tests to pass:

1. Identity: two studies reusing a run ID; same-study replay; relative-path
   equivalence and separation; study-scoped cost backfill without overwrites.
2. Migration: one-time orphan cleanup and exact accounting after replay;
   restrictive legacy-table upgrade retaining study metadata.
3. Coverage: complete task×trial grid; missing cells; unscored attempts; scored
   zero observations; complete-arm positive control.

Two guard tests cover ordinary/benchmark usage separation with exact positive
counts and manual override/model metadata preservation. Both guards must pass
for any score. The verifier checks each Node process return code and exact
matched/pass/fail/cancelled/skipped test counts. A launch/setup failure cannot
satisfy a bucket.

The migration oracle intentionally discards pre-release benchmark rows with null
study identity. The acceptance contract permits that specific one-time cleanup
and tests accounting after replay; it does not demand preserving those orphans
while using an oracle that deletes them. Already identified studies and ordinary
activity are preservation controls.

## Verification on 2026-09-15

Host `macbook`; Node `v24.13.0`; existing host-native AgentMonitor dependencies
(`better-sqlite3` 13.0.2, `tsx` 4.23.5). Real SQLite files are created under a
unique temporary directory; DB path is asserted before use and any destructive
fixture. No model calls, network services, or install database access.

| Variant | Identity | Migration | Coverage | Guards | Score |
|---|---:|---:|---:|---:|---:|
| Untouched | 1/4 | 0/2 | 0/2 | 2/2 | 0.0000 |
| Identity repair only | 4/4 | 0/2 | 0/2 | 2/2 | 0.3333 |
| Migration repair only | 1/4 | 2/2 | 0/2 | 2/2 | 0.3333 |
| Coverage repair only | 1/4 | 0/2 | 2/2 | 2/2 | 0.3333 |
| Identity + coverage | 4/4 | 0/2 | 2/2 | 2/2 | 0.6667 |
| Historical first repair | 2/4 | 0/2 | 1/2 | 2/2 | 0.0000 |
| Full oracle | 4/4 | 2/2 | 2/2 | 2/2 | 1.0000 |

Additionally, all **36 upstream tests** in `benchmark-import.test.ts`,
`benchmark-queries.test.ts`, `benchmark-usage-segregation.test.ts`, and
`schema-migrations.test.ts`, `cost-backfill.test.ts`,
`codex-model-backfill-migration.test.ts`, and
`skill-context-backfill-migration.test.ts` passed on the oracle snapshot (including HTTP route
checks using ephemeral localhost ports). No cancellation or skipped tests.
OpenBench `validate_tasks` also passed its own untouched/oracle polarity check.
A mutation that removes default benchmark segregation fails the guard and forces
a zero score even with the other repairs installed. Missing-dependency preflight
was checked separately: exit 77, no score. Local raw logs and `validation.json`
record the executions.

The historical first repair is useful: it fixes study namespacing and unscored
single-task exclusions but still fails equivalent relative-path handling,
legacy-table and orphan migration, and multi-task coverage. Its score is zero
because no whole bucket is complete; per-test results preserve that progress.

## Execution limits and next gate

This is a fork-local native-dependency task matching the existing
`tasks-local/am-consistency-pr80` execution shape. `AGENTMONITOR_DEPS` must point
to an AgentMonitor `node_modules` built for the verifier's Node/OS/architecture;
the checker defaults to `/Users/dg-mac-mini/Dev/agentmonitor/node_modules`.
Missing dependencies or an incompatible native SQLite binary return 77 and no
score. The checker never installs or rebuilds dependencies. It copies only the
candidate `src/` into a disposable directory and supplies pinned package metadata
and hidden tests there.

No Harbor Docker environment, Mac Mini runtime validation, or frontier-model
calibration has been performed. The complete backend includes service imports
whose execution depends on the native environment. Provision the matching
runtime before a scored campaign. Difficulty is a provisional hard classification
from interacting review failures, not a measured frontier failure rate.
