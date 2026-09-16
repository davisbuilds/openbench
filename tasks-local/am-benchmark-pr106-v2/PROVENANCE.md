# AgentMonitor benchmark accounting — PR #106, contract revision 2

This is a versioned correction of `am-benchmark-pr106`, not a harder variant and
not an untouched historical checkout. No difficulty claim is made before fresh,
properly isolated calibration. Revision 1 and its results must remain unchanged.

## Source and license

- Public repository: https://github.com/davisbuilds/agentmonitor
- Reviewed PR: https://github.com/davisbuilds/agentmonitor/pull/106
- Snapshot base and reference: `d33ef01584fe78f686ef921995f55e0ce70b9760`.
- Pre-review defective files: `4cfbddaa788dddb205d19225135cab44578f5fbe`.
- The original package declares ISC and author `davisbuilds`; metadata is retained
  verbatim. That commit has no standalone LICENSE file; none is invented here.

`workspace/`, `solution/` and `SOURCE_MANIFEST.json` are byte-identical to revision
1. The full backend comes from the reference commit, with four source files
restored to the pre-review version: `src/import/benchmark.ts`, `src/db/schema.ts`,
`src/db/queries.ts`, and `src/db/v2-queries.ts`. The solution restores those four
files to the reference. The source manifest records each source revision/hash.

## Contract corrections

A native exploratory screen exposed hidden assertions about implementation
choices that were not clearly specified by the user-facing instruction. Revision
2 removes those incidental requirements while retaining the three behaviors:

1. **Identity:** same-study replay, separate explicit studies reusing a run ID,
   equivalent absolute/bare/parent-relative file paths, different named sibling
   directories, original run-ID metadata, scoped cost backfill and no overwrite.
   Literal basename IDs and distinct internal session IDs are not required.
2. **Startup upgrades:** build an old database with raw fixture SQL before any
   candidate initialization. Call the real `initSchema()` startup entry point,
   replay, verify exact cost accounting and preservation, close/reopen, and
   replay again. Do not fabricate an old installation by resetting one marker
   after the candidate has initialized its own migration state.
3. **Coverage:** missing/unscored exclusions and scored-only means across the full
   task×trial grid. Verify each arm's `n`, `mean_score`, and `excluded_trials`;
   do not force a changed interpretation of `expected_trials`.

Same-named directory paths are outside this revision's promised legacy identity
contract: the historical reference derives a basename. A task requiring that
stronger identity behavior needs a separately repaired reference and version.
The instruction now says different named sibling directories explicitly.

Both ordinary/benchmark usage separation and manual study override/metadata
preservation remain guards. Each whole behavior bucket earns one third only if
all its checks pass; guards must pass to award any score. The checker requires
exact matched/pass/fail/cancelled/skipped counts and successful process exit.

## Offline control validation

Run from the repository root with the checker runtime provisioned:

```sh
python3 tasks-local/am-benchmark-pr106-v2/validate_controls.py \
  --output results/am106-v2-validation
python3 -m obench validate --tasks-dir tasks-local/am-benchmark-pr106-v2
```

The control program uses disposable workspace copies and records every stdout,
stderr, exit status, bucket result and input digest under the supplied local
results directory. It also verifies revision 1 stayed byte-identical.

Expected and observed controls on 2026-09-16 (`macbook`, real host-native SQLite):

| Variant | v2 score | v1 score when checked |
|---|---:|---:|
| Untouched | 0.0000 | — |
| Full historical reference | 1.0000 | — |
| Identity repair only | 0.3333 | — |
| Migration repair only | 0.3333 | — |
| Coverage repair only | 0.3333 | — |
| Identity + coverage | 0.6667 | — |
| Canonical absolute directory IDs | 1.0000 | 0.6667 |
| Startup cleanup with its own receipt table | 1.0000 | 0.6667 |
| Raw run-ID sessions and trial-count summary | 1.0000 | 0.3333 |
| All three valid alternatives together | 1.0000 | 0.0000 |

These alternative controls are implementation changes, not weakened fixtures.
The same submitted bytes are rejected by v1 and accepted by v2. In particular,
absolute IDs canonicalize real paths so macOS `/var` and `/private/var` aliases
remain the same directory. An initial noncanonical absolute-ID control failed
path equivalence; canonicalizing it made the real behavior pass.

The reference/alternative acceptance and partial mutants establish offline
checker behavior, not frontier model difficulty. Preserve individual test
outcomes beside whole-bucket scores when analyzing later model trials.

## Execution and isolation

This remains a native-dependency task. `AGENTMONITOR_DEPS` must identify a
host-compatible AgentMonitor `node_modules` with `better-sqlite3` and `tsx`.
Missing/incompatible dependencies exit 77 with no score; the checker installs
nothing. Tests run on disposable SQLite files, never the live install database.

The model should receive only the instruction and workspace. Its filesystem
must not expose this task's solution, checker, controls, provenance, sibling
checkouts, history, or previous attempts. Native `workspace-write` and isolated
HOME alone do not establish that read boundary. Do not use the unrestricted
native host lane for difficulty admission. Harbor/environment packaging and a
paired allowed/denied read probe are required before a fresh calibration cohort.
