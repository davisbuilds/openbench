# Private evaluation packages

Keep reusable OpenBench infrastructure public and private evaluation content in
an operator-owned repository. The public test suite contains synthetic controls
only. Previously public cases are development material, not unseen holdouts.

This first interface freezes inputs, exports a solver projection and replays
frozen submissions. **It cannot authorize a campaign or produce an importable
benchmark result.** Suite/Harbor/admission/import integration is the next slice.
Do not bypass campaign gates to use these diagnostic scores as benchmark runs.

## Layout and authority

```text
case/
  evaluation.json
  evaluator.py
  cases.json
  solver/
    instruction.md
    workspace/web/...
  fixtures/...                 # optional evaluator-only byte resources
  controls/reference/web/...   # optional development reference
```

Descriptor (unknown fields and worker backends are rejected):

```json
{"schema":1,"id":"synthetic-viewer","case_revision":1,"oracle_revision":1,
 "split":"development","backend":"activity-explorer-v2"}
```

Case and oracle revisions advance independently under
[benchmark versioning](project/BENCHMARK_VERSIONING.md). The full archive hash
binds both, including fixtures, control repairs, solver inputs and file modes.
Archives are deterministic, bounded, uncompressed tar files with hashed
manifests. Links, special files, traversal, duplicate entries and extra roots
are rejected. Existing archives and exports are never overwritten.

`cases.json` is a nonempty list of up to 128
`{"id":"list","bucket":"content","request":{...}}` records. IDs are unique.
Requests use the selected public worker's protocol. The first supported backend
is `activity-explorer-v2`: its isolated-world browser observer remains public;
case inputs and acceptance rules may be private. Ordinary project tests and
configuration belong in the solver checkout when needed for realistic work.

The private `evaluator.py` defines:

```python
def grade(cases, observations, fixtures):
    # cases: fresh JSON copies of the declared case records
    # observations: confined worker results, in the same order
    # fixtures: relative names mapped to bytes from fixtures/
    return [{"pass": False, "reasons": ["required-behavior-missing"]}
            for case in cases]
```

Each verdict requires a boolean `pass` and at most 16 failure reasons, each
1–200 characters. Passing verdicts have no reasons; failing verdicts have at
least one. Invalid verdicts or evaluator exceptions make evaluation incomplete.
Worker failure cannot be reported as successful behavior. OpenBench computes
the fraction of entirely passing buckets; it is not percent implementation or
design preference. Check identities come from the frozen declarations.

**Evaluator Python is trusted host code, not sandboxed code.** The operator must
review and explicitly approve the package hash. Do not accept an archive or hash
chosen by a solver. Imports, file access and subprocesses in evaluator Python
have the operator's authority. Keep evaluator dependencies to the standard library
and sealed public backend helpers; other imports, ambient files or network data
are not frozen by this interface and undermine reproducibility. Avoid them.
Inspection/export compile syntax but never execute evaluator Python.

Only source files and case requests reach the grading container. Evaluator code,
reference solutions and auxiliary fixtures remain on the host; cases expose
their ordinary application input data to the rendered candidate as needed.
The evaluator receives a captured archive snapshot. Replay rejects a changed
archive or public implementation before publishing completion.

## Commands

```bash
obench repair package freeze PRIVATE_CASE_DIR --output PRIVATE_PACKAGE.tar --json
obench repair package inspect PRIVATE_PACKAGE.tar --sha256 SHA256 --json
obench repair package export PRIVATE_PACKAGE.tar --sha256 SHA256 \
  --destination FRESH_SOLVER_EXPORT --json
obench repair package replay PRIVATE_PACKAGE.tar --sha256 SHA256 \
  --trust-evaluator --source FROZEN_WEB_SOURCE_ROOT --image sha256:IMAGE \
  --output PRIVATE_REPLAY.json --json
```

`export` writes only `instruction.md` and `workspace/`. It does not write a
Harbor task, evaluator selector, hidden cases or grading receipt. Review the
explicit solver namespace before freezing: namespace separation cannot detect
an author deliberately or accidentally placing an answer in solver-visible text.
The package archive itself must never be mounted/copied into a solver container.

Replay accepts a frozen root containing only `web/`. Its report and the adjacent
`.evidence` operation directory are private, newly created files. Reports bind
package, source, public implementation and immutable runtime identity; include
observations and failed checks; and explicitly set `campaign_eligible: false`.
`obench repair status PRIVATE_REPLAY.json.evidence --json` detects unfinished or
interrupted operations. Keep these reports private: they reveal evaluation data.

Exit codes: **0** completed successful operation / solved replay; **1** completed
unsolved replay; **2** invalid input or incomplete execution. With `--json`, stdout
contains one JSON report; normal evaluator print output is suppressed. Package
replay never discovers evaluators from task metadata, environment or search paths.

The credential-free boundary control uses a development package with
`controls/reference/` matching the solver's `web/` files:

```bash
python scripts/local/verify_evaluation_package.py \
  --archive PRIVATE_PACKAGE.tar --sha256 SHA256 --trust-evaluator \
  --image sha256:IMAGE --output PRIVATE_CONTROL_EVIDENCE
```

It exercises real Harbor sandbox execution, workspace writes, direct/symlink/
subprocess denial of a known-present host archive, confirmed solver termination,
and baseline/reference replays. It makes no model calls and does not establish
actual-harness qualification, a campaign-quality receipt or task difficulty.

## Personal evaluation policy

Authoring/verifier sessions may inspect private assets. Evaluated sessions must
start fresh without that conversation, memory, credentials or repository history.
Package only approved day-to-day guidance and skills in the separately frozen
context bundle. Development cases support prompt/harness tuning; cases inspected
for that purpose must leave the held-out comparison pool. Runtime isolation does
not establish that a model has never encountered a public task or reference.
