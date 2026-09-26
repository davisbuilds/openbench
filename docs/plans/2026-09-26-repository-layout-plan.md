---
date: 2026-09-26
author: Codex
topic: repository-layout
stage: plan
status: completed
source: User request to organize the root and similarly named directories
risk_profile: routine
readiness: ready
---

# Repository Layout Plan

## Goal

Make the checkout easy to navigate, with one location for task collections,
maintained utilities, experiment inputs, and documentation.

## Scope

Group task collections under `benchmarks/`, consolidate utilities under
`scripts/`, move historical launchers/ablations under `experiments/`, and move
long-form root documentation into `docs/`. Remove deprecated file-entry shims
whose supported replacements are the installed CLI and Python modules.

## Assumptions And Constraints

- Baseline is `4054feb`. The MacBook edits source; the inactive Mini syncs Git.
- Do not change task payload bytes, native dependency locks, grading logic,
  published release bytes/URLs, private captures, or ignored run evidence.
- Keep custom-project `tasks/` and `.openbench/tasks/` discovery working.
- Historical run paths remain provenance. New commands use the new paths;
  there are no compatibility symlinks or edits to old sealed records.
- Existing runtime admission must be renewed before a new campaign because
  referenced control paths change. This cleanup launches no model campaign.

## Task Breakdown

### Task 1: Consolidate the tree

**Objective**: Reduce root clutter and group related assets.
**Files**: `README.md`, `docs/README.md`, `docs/project/FORK_WORKFLOW.md`,
`AGENTS.md`, `pyproject.toml`, `.publication-hygiene.json`, task collections,
root reports and historical helper directories.
**Dependencies**: None.
**Assumptions Verified**: `obench/paths.py` owns legacy task discovery;
`obench/publish.py:task_content_digest` hashes task-relative content;
`docs/README.md` declares `/docs` as the published site root.
**Implementation Steps**: Capture a byte inventory; move assets by purpose;
update maintained navigation and references; retain historical evidence bytes.
**Verification**: Compare moved task/file bytes to the captured baseline and
inspect the tracked root inventory and local document links.
**Done When**: Every moved asset has one documented home and task bytes match.

### Task 2: Repair consumers

**Objective**: Make built-in commands and tests use the organized checkout.
**Files**: `obench/paths.py`, `obench/site.py`, `.openbench/suites/openbench-lite.toml`,
`.github/workflows/ci.yml`, `.github/workflows/harbor-integration.yml`,
`scripts/local/`, `obench/tests/`, `experiments/specs/`.
**Dependencies**: Task 1.
**Assumptions Verified**: Installed projects use their own task roots; runtime
admission includes control script/task paths and cannot silently reuse old evidence.
**Implementation Steps**: Add grouped-layout discovery without changing installed
project defaults; update repository paths and script-relative imports.
**Verification**: Focused discovery/CLI/suite tests, complete offline CI commands,
publication guard, and hosted Docker/Harbor integration.
**Test Discovery Verified**: `python3.13 -m unittest discover -s obench/tests -v`
is the repository's runner; new discovery cases belong under `obench/tests/`.
**Done When**: New checkout defaults and existing custom-project layouts work.

### Task 3: Deliver and document

**Objective**: Ship a reviewed cleanup with a clear navigation guide.
**Files**: `docs/project/REPOSITORY_LAYOUT.md`, this plan, PR description.
**Dependencies**: Task 2.
**Implementation Steps**: Open the fork PR, request Codex review, address findings,
merge on green CI, and fast-forward the clean inactive Mini checkout.
**Verification**: Exact-head CI, resolved review threads, clean synced checkouts.
**Done When**: The reorganization is merged and the remaining run gate is explicit.

## Risks And Mitigations

Path literals are widespread: update live consumers and exercise actual commands.
Frozen task/report content is not a search-and-replace target. Preserve historical
paths inside evidence and keep source history available for historical replay.
Ignored directories may contain worktrees or virtual environments: leave them in
place. Rollback is an ordinary Git revert; no state/history migration is involved.

## Verification Matrix

| Requirement | Proof command | Expected signal |
| --- | --- | --- |
| Frozen payloads | Baseline byte inventory comparison | Every task payload unchanged |
| Discovery | Focused unit tests and CLI validation | Grouped and custom roots resolve |
| Integration | Offline CI and Harbor integration | Green checks without inference |
| Publication | Staged publication guard and link audit | No new leaks or moved-link breaks |
| Delivery | GitHub PR and Git status | Reviewed merge; clean synced main |

## Delivery

Merged in [PR #18](https://github.com/davisbuilds/openbench/pull/18) at
`176d71f`. MacBook and inactive Mini checkouts fast-forwarded to the merge.
Codex reviewed `1a0cc48`; both path findings were corrected in `0eb64a6`,
and the inline thread was resolved. A nested pack-discovery fix is in `4f56406`.

Final-head CI passed Python 3.11 and 3.13 (1,933 tests each, five expected skips),
checker validation, publication hygiene, workflow checks, and credential-free
Docker/Harbor integration. All 1,795 task files remain byte-identical; historical
receipts, captures, datasets, and published artifacts were preserved. The
maintained ablation probe received path repairs without running inference.

The next campaign still requires fresh runtime qualification. Old paths remain
valid provenance for their recorded checkout; results must be interpreted with
their task, grader, harness, and runtime versions, not as isolated scores.
