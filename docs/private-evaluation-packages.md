# Private evaluation packages

Keep reusable OpenBench infrastructure public and private evaluation content in
an operator-owned repository. The public test suite contains synthetic controls
only. Previously public cases are development material, not unseen holdouts.

Package inspection and replay remain diagnostic. The canonical campaign route
additionally requires an explicit operator pin, evaluator trust, sealed task,
actual-harness workflow evidence, fresh quality replay and runtime admission.
A diagnostic replay report alone cannot authorize a campaign.

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
Requests use the selected public worker's protocol. The browser backend
is `activity-explorer-v2`: its isolated-world browser observer remains public;
case inputs and acceptance rules may be private. Ordinary project tests and
configuration belong in the solver checkout when needed for realistic work.

The browser request requires `mode` (`list`, `loading`, `retry`, `detail`,
`keyboard`, or `layout`) and a `data` array of records with string `id`, `title`,
`project`, `status`, and `description` fields. IDs and case-insensitive titles
must be unique; titles/IDs are nonempty and statuses are `running`, `completed`
or `failed`. Titles must already use canonical accessible-name whitespace:
single spaces without leading/trailing whitespace, soft hyphens, byte-order
marks or zero-width spaces. The fixed backend uses anchored accessible-name locators; unsupported
titles are rejected rather than silently changing the evaluation input.
Detail/keyboard modes need at least one record. Optional `query`
is a string; `status` is `Running`, `Completed` or `Failed` (omit for All).
Filters are supported only in list/loading/retry modes: the other probes address
the original records directly. `largeText` is a boolean for layout mode only.
Optional `viewport` requires integer width/height from 1 through 4096; absent
viewport uses the worker default. Unknown request keys are rejected. Invalid
requests fail freeze/load and replay before candidate execution, never as a
candidate miss. These protocol checks do not replace task-quality controls.

### Review artifacts

`artifact-review-v1` observes a review report as data; it never executes submitted
source or starts a browser. Every case request is exactly `{"mode":"review"}`.
Cases and acceptance rules remain evaluator-owned, with at most 64 cases.
The solver workspace must contain `submission/review.json`. Seed it with `{}`
for a completion control: an empty, valid clean review is `{"findings":[]}`.

The complete report has exactly this shape:

```json
{"findings":[{"title":"[P1] Handle absent input","body":"Explain the triggering condition and its consequence.","file":"src/example.py","line_start":2,"line_end":2,"priority":1}]}
```

All six finding fields are required. File paths are relative without traversal;
line ranges are positive and ordered; priority is an integer 0 through 3.
There are at most 64 findings, titles at most 512 characters, bodies at most
16,000 characters and paths at most 512 characters. The entire UTF-8 artifact
is bounded to 64 KiB. Duplicate keys, nonfinite numbers and unknown fields fail.
Schema validation does not establish that findings are correct or well located.

The fixed observer returns `{"ok":true,"value":{"findings":[...],"scope_drift":[]}}`.
It compares the complete submitted workspace to the approved initial file hashes,
excluding `submission/` outputs and generated `.git/` state. The runtime's `.git`
file is exempt only for the exact pointer `gitdir: /tmp/openbench-workspace.git`
followed by a newline; altered pointers remain source drift. Added, removed and
changed source bytes are recorded in `scope_drift`. Any scope drift forces all
affected checks to fail even if the evaluator returns pass. File permissions
are not part of this byte comparison. Run checks that create caches or scratch
outside the source tree. Malformed or missing reports return `ok:false`; these
are candidate misses, not infrastructure exclusions. Evaluators must handle
failed observations and return their normal bounded verdicts.

Reference controls live under `controls/reference/` and may overlay only the
report onto the original reviewed source. Retain baseline, two byte-distinct
valid outputs and targeted invalid reports for every scoring bucket. Two clean
reports differing only in whitespace prove accepted encodings, not independent
semantic implementations. Quality receipts validate the declared screening
controls; neither lexical screening nor report validity alone establishes
semantic review quality. Preserve independent semantic adjudication as separate
evidence when making that claim.

### Trusted evaluator

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

Browser replay accepts a frozen root containing only `web/`; review replay takes
the complete frozen workspace so source drift remains observable. Its report and the adjacent
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


## Canonical campaign route

Export a sealed Harbor task from the approved solver projection. This operation
validates syntax and hashes but does not execute the evaluator:

```bash
obench repair package export-task PRIVATE_PACKAGE.tar --sha256 SHA256 \
  --destination .openbench/tasks/CASE-c1-o1 --json
```

Use the descriptor's ID and independent case/oracle revisions for the directory
name. The generated task uses digest scheme 5; historical schemes stay readable.
The archive itself must remain outside the task and solver checkout.

The operator's local-only suite supplies all three authorization fields:

```toml
[sandbox]
kind = "repair-v1"
runtime_image = "sha256:REPLACE_WITH_IMAGE_DIGEST"
evaluation_archive = "REPLACE_WITH_PRIVATE_ARCHIVE_PATH"
evaluation_sha256 = "REPLACE_WITH_ARCHIVE_DIGEST"
trust_evaluator = true
```

Archive paths resolve relative to the project root, like context archives.
Each selected task must match this package's solver projection and revisions.
The archive hash and descriptor are sealed into suite intent; its local path
appears only in execution configuration. Verifier and environment pins must
agree. The host verifier checks task/package bindings before and after source
freezing and grading. Candidate code cannot choose a host module or evaluator.

Use the same `--evaluation-archive`, `--evaluation-sha256`, and `--trust-evaluator`
flags with `obench repair inspect|replay|validate` and the actual-harness workflow
probe. Repair quality requirements are unchanged: untouched baseline, at least
two distinct valid implementations, targeted defective controls covering every
bucket, and a passing project workflow. Admission replays these controls rather
than trusting saved scores. Package changes invalidate task, workflow, quality
and runtime evidence.

`obench campaign qualify` tests the actual package through each selected Codex
model configuration using offline transport, including direct/symlink/subprocess
reads of a known-present host archive. It also exercises the package's reference,
malformed-source and forged-reward Harbor lifecycle, followed by the existing
short authenticated public file-edit controls. These are runtime qualification,
not benchmark model attempts. Qualification requires a
**development** package with reference controls matching its backend. Browser
packages require `controls/reference/web/`; review packages require
`controls/reference/submission/review.json`. Review qualification uses the
non-browser observer, report export, malformed-artifact and forged-reward
controls, while retaining private archive denial and native tool-loop checks.
Use independently
reviewed held-out content only after its qualification policy is established.

Then use ordinary `obench campaign launch --admission ... --quality ...`.
No diagnostic command bypasses those gates. Keep archives, detailed check reports,
transcripts and suite results private. The reusable public CI control is
`scripts/local/verify_package_campaign.py` and contains only synthetic material.
