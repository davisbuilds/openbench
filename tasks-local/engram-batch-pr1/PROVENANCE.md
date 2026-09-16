# Engram batch curation — PR-derived candidate

## Source and license

- Public upstream: https://github.com/davisbuilds/engram
- Review/fix family: https://github.com/davisbuilds/engram/pull/1
- Final reference commit: `92b2d854404832302dc2be62a97fec382e57ca60`
  (tree `b43ce1b0e9cdcbc67bc712f8676cef8b8701f74e`).
- Earlier reviewed implementation: `d1daf32f5d8e0ec7d0a634a105d818bd22157b54`
  (tree `2e5160308ef0d22b7e0ac439cc311de3f4628ac3`).
- MIT; original notice retained in `LICENSE` and `workspace/LICENSE`.

## Exact construction

The workspace contains every non-test Go source file from the final reference
commit, plus its `go.mod`, `go.sum`, `Makefile`, `.golangci.yml`, and license.
Only `internal/curate/curate.go` comes from the earlier commit, with precisely
one compatibility edit: `lock.Acquire(root, lock.DefaultStaleAfter)` becomes
`lock.Acquire(root)`. This keeps the final advisory-lock API and implementation.
The solution overlays the unmodified final `internal/curate/curate.go`.
`checker_data/source-manifest.json` records SHA-256 for every workspace file.

This plants exactly two corrected behaviors: rescope uses the initial corpus
instead of the current result of earlier operations; Apply ignores Discover's
parse-error slice. Other PR findings (unrelated merge targets, intra-batch name
conflicts, current-state validation, and lock behavior) are regression controls,
not scored planted defects. No source repository was modified to construct it.

Repository docs, agent instructions, skills, CI configuration, and Git history
are omitted to avoid exposing fix commentary or unrelated harness instructions.
Every upstream test is excluded from the agent workspace and retained only in
`checker_data/upstream/`. The implementation still includes all CLI callers and
all supporting packages, rather than an isolated function puzzle.

## Oracle and scoring

The checker copies the submitted workspace into a fresh temporary directory,
removes candidate test files from that copy, installs the trusted curate tests,
and runs `go test -json -count=1 -timeout=45s ./internal/curate`.
It parses explicit named test completion events and requires the process and
package result to agree. Missing/skipped tests, compile failures and timeouts
cannot produce a passing score. It does not parse candidate output for scores.

Two equally weighted buckets:

1. **Ordered batch state:** historical update/rescope regression plus five
   variants: update, merge with source-name reuse, merge into a new identity,
   add, and repeated scope changes. Checks all persisted memory fields and
   byte preservation of an unrelated memory; merged sources must disappear.
2. **Malformed canonical store:** historical refusal regression plus twelve
   cases combining three parse failures with unrelated add, overwrite add,
   merge onto malformed target, and removal of a valid memory. Every refusal
   must preserve the full canonical Markdown file set and exact bytes. Removing
   only the malformed input must let the identical operation succeed.

All fourteen remaining upstream curate tests must pass before either bucket
gets credit. They cover valid operations and refusals, merge collision rules,
batch validation, current-disk validation, lock ownership, and proposal parsing.
Scoring is 0, 0.5, or 1; only 1 exits zero. A rejection of all batches scores zero.
This does not assert rollback of arbitrary mid-write I/O failures, crash recovery,
or solve every finding in PR #1.

## Runtime and reproduction

Requires Python 3, Go >=1.26, and the cached module
`gopkg.in/yaml.v3@v3.0.1`, pinned by the reference `go.mod` and `go.sum`.
The checker sets `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `GOWORK=off`,
`GOENV=off` and clears `GOFLAGS`. Provision the toolchain and module cache before
an evaluation; missing Go or dependencies exits 77 with an explicit SKIP and no
score. Missing code or a broken submitted module file fails as a candidate error.
No live model, agent, service, personal corpus, or network dependency is used.
The Makefile is source context; its formatter/linter are not checker prerequisites.

From OpenBench:

```sh
python3 -m obench.validate_tasks --tasks-dir tasks-local/engram-batch-pr1 --no-imported
```

The checker itself runs with the submission as cwd and `TASK_DIR` set to this task
folder. Keep `checker_data`, `solution`, provenance and validation outside the
agent-visible workspace.

## Observed validation — 2026-09-15

On macOS arm64 with Go 1.27.1, offline and uncached test execution:

| Variant | Exit | Score | Controls |
| --- | ---: | ---: | --- |
| Untouched curated workspace | 1 | 0.0 | Pass |
| Ordered-state fix only | 1 | 0.5 | Pass |
| Parse-error refusal fix only | 1 | 0.5 | Pass |
| Full reference overlay | 0 | 1.0 | Pass |
| Deliberately reject every batch | 1 | 0.0 | Fail |
| Empty module cache | 77 | No score | Not run |

The OpenBench task validator passes baseline/golden polarity. The full reference
workspace with every upstream test restored and the additional behavioral tests
passes `go test -count=1 ./...` across all packages. Raw outputs are recorded in
`validation.json`. The two partial variants each remove only the corresponding
fix from the reference behavior; neither depends on the other bucket passing.

## Admission limitations

This is a compatibility snapshot/checker candidate, matching the existing local
PR-derived tasks. It has no pinned Harbor Docker environment yet. Provisioning,
container isolation, read-only hidden-oracle placement and execution on the target
benchmark machine remain campaign integration work. The code was reconstructed
and tested locally; it has not been calibrated on Terra, Luna, or another model.
Difficulty is therefore provisional. Two historical defects with wider behavioral
coverage may still be easy for frontier models; call this a candidate golden case
until repeated clean-harness trials demonstrate discriminating power.

## Independent verifier review — 2026-09-15

An independent pass found and corrected an expectation-aliasing weakness in the
additional ordered-state test. The expected memory had been copied from the
proposal after `Apply`, allowing an implementation that mutated its input
metadata to alter the expected result too. The test now freezes a deep copy
before calling candidate code and uses that independent identity for readback.
A deliberate description-corruption mutation scored 1.0 before the correction;
it now fails the ordered-state bucket and scores 0.5.

After the correction, independent OpenBench polarity again passed 0.0 → 1.0;
an ordered-state-only repair scored 0.5, blanket rejection scored 0 with failed
controls, and a compile-error submission scored 0 with missing-test diagnostics.
The full reference source plus all upstream and additional tests again passed
`go test -count=1 ./...` offline. `independent-review.json` records these probes.
The workspace manifest was independently verified: 34 files, 124137 bytes, no
symlinks, tests, histories, or oracle artifacts in the submitted workspace.
