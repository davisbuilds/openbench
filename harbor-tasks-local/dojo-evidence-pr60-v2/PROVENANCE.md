# Provenance — dojo-evidence-pr60-v2

This file retains the original source/construction history. The Harbor v2
packaging, oracle correction, and new offline admission procedure are described
in [README.md](README.md). Original recipe and validation references below refer
to the frozen native task and its historical evidence, not files shipped in this
package. The original source/checker contract digest was
`9a28649aaa30b6244c2d1829762eaf7de14f59d106b55d6c64396401d3c82449`;
v2's corrected native-contract digest is
`afab14cc6df0f9a0e056cd866f294b290545bc880938706a49f86f5e8778b421`.

**Provisional task; difficulty remains unmeasured until repeated calibration.**
This local draft packages three behavioral regressions from the user's public
[Dojo PR #60](https://github.com/davisbuilds/dojo/pull/60). No model run was used
to construct or validate this draft.

## Pinned source

- Repository: [davisbuilds/dojo](https://github.com/davisbuilds/dojo).
- Earlier source: [`e0163e0fc489e6fee0b469a84fe80c2116e2ddda`](https://github.com/davisbuilds/dojo/commit/e0163e0fc489e6fee0b469a84fe80c2116e2ddda).
- Reviewed fix: [`6250a0b152eec023678d0da53fa6850536fba954`](https://github.com/davisbuilds/dojo/commit/6250a0b152eec023678d0da53fa6850536fba954),
  “Address PR #60 review: surface gating, block extraction, duplicate identities.”
- Source files: `scripts/profiles/*.py`. The snapshot omits upstream tests,
  fixtures, documentation, skill instructions, and historical prompts.
- Dependency: PyYAML 6.0.3, matching the pinned source's `requirements.txt`.
  The checker itself uses Python's standard-library `unittest`.

### License status

The pinned Dojo tree has no repository-root license file, and the extracted
`scripts/profiles` files contain no explicit copyright/license notices. No MIT
or other redistribution grant is inferred from public visibility. This is an
authorized derivative of the user's own public repository. This task adds no
new license grant. The construction script preserves
copyright, SPDX, and license notices if present in source.

## Construction and mutation recipe

`build_recipe.py /path/to/dojo` reads pinned Git objects without changing the
source checkout and reproduces `workspace/` and `solution/`:

1. Start with the final `scripts/profiles` snapshot.
2. Replace `rollout_codex.py` with the earlier version, then retain final
   `observations()` to preserve its `errors=` argument and parse-error handling.
   The two remaining regressions accept a skills block from any record and
   compare bare-name sets. No fourth, unscored scan regression is introduced.
3. In final `budget.py`, remove the `accepts_surface` guard from `assess` and
   its condition from `Assessment.gating`. Keep the `surface` argument,
   dataclass field, policy method, and other public interfaces intact.
4. Blank narrative docstrings and comments in the model workspace, retaining
   executable statements and any legal notices. This removes historical bug
   explanations that would reveal the repairs. No real transcript is copied.
5. Place the complete, unchanged final `rollout_codex.py` and `budget.py` under
   `solution/scripts/profiles/` as the hidden reference overlay.

The model receives `instruction.md` and the source workspace. Provenance,
construction script, checker, hidden tests, solution, and validation logs remain
outside that workspace. Public source/fix availability is a contamination risk;
this task makes no claim that training or retrieval exposure is impossible.

## Behavioral oracle

Each bucket has four required unittest methods and contributes one third only
when every method passes. Exact executed test identities and counts must match;
missing, skipped, zero-match, import-error, and timeout results cannot pass.

| Bucket | Required behavior | Positive control |
|---|---|---|
| `rollout` | Ignore user, assistant, tool, and compacted listing decoys; use developer context | Developer listing accepted; empty developer listing differs from no observation |
| `budget` | Wrong or absent surface is unsupported and cannot gate, including direct assessments | Declared surface is deployable, has positive measured demand, and can gate |
| `mismatch` | Preserve origin-qualified identities and duplicate multiplicity | Equivalent reordered listings, root aliases, and connector version paths compare equal |

Every JSONL record and skills listing in the oracle is synthetic. No upstream
session fixtures, transcript text, account identifiers, or user prompts are used.
The tests exercise the public `read_rollout`, `assess`, `Assessment.gating`, and
`surface_mismatch` behavior; they do not require a particular repair strategy.

`checker.sh` runs from the candidate workspace with `TASK_DIR` set to this task
directory. Set `PR_EVAL_PYTHON` to a Python interpreter with PyYAML installed.
Missing interpreter or PyYAML exits 77 (environment skip). No dependency is
downloaded and no live harness/model is called by the checker.

## Offline validation

See `validation.json` and the named stdout/stderr files for measured results:

- Buggy snapshot: all buckets fail, score **0.0000**.
- Each isolated repair: exactly its bucket passes, score **0.3333**.
- Each pair of repairs: exactly those buckets pass, score **0.6667**.
- All repairs and the full reference overlay: all 12 methods pass, score **1.0000**.
- Empty test selections against the reference overlay: score **0.0000**, exit 1.
- Missing interpreter and missing PyYAML probes: exit **77**.
- `python3 -m obench validate --tasks-dir <this-task>`: polarity validation passed.

Validation establishes checker polarity and independent credit, not model
difficulty. Repeated runs with pinned harness/model settings are still needed
before admitting this task to a calibrated golden set.
