# Repair quality and operator verification

Runtime qualification proves an execution route. Task quality controls prove
specified properties of a task and grader. Neither establishes benchmark
hardness. Isolated repair campaigns require **both**, plus a workflow probe for
each exact task/image pair.

## Discover and inspect

```sh
obench repair --help
obench repair inspect /private/tasks/repair-case --json
```

Inspection validates the task seal and reports case/oracle identity, source
scope, named checks, scoring buckets, known eligibility restrictions, and the
next operation. It needs no Docker daemon or model credentials. Historical Dojo
oracles 3–5 remain replayable but are ineligible for new quality admission: their
synthetic version-path fixtures do not establish a representative contract.

## Replay a saved repair

```sh
obench repair replay /private/tasks/repair-case \
  --source /private/frozen-submission --image sha256:IMAGE \
  --output /private/replay.json --json
```

This runs the **production isolated grader** against the permitted source subtree.
It never imports candidate code on the host, calls a model, or modifies old
results. A full checkout or frozen source directory may supply that subtree;
this is not certification of files outside the submitted scope. Reports identify
source hashes, task binding, runtime image, grading implementation, named checks,
buckets and verdicts. Dojo includes expected/observed values; registered oracles
include observations and their existing named assertions. Worker errors remain
incomplete evidence, distinct from a completed wrong answer. Invalid candidate
artifacts cannot establish a successful negative quality control.

JSON is operator-only: it can contain hidden grading details and submitted code
outputs. Keep reports, controls and reference repairs outside the solver context
and public exports. Output files are private and exclusively created; choose a
new path for each attempt rather than overwriting evidence.

## Validate quality controls

First run the credential-free actual-Codex workflow probe. It uses a fake provider
and requires an empty output directory under ignored `results/`:

```sh
python scripts/local/verify_repair_codex.py \
  --task /private/tasks/repair-case --runtime-image sha256:IMAGE \
  --model gpt-6-sol-high \
  --project-check 'pnpm exec tsc --noEmit && pnpm lint && pnpm build && pnpm test' \
  --output-dir results/workflow-example
```

Choose commands appropriate to that checkout. Exercise the advertised full build
and tests where supported. A buggy baseline may legitimately fail repair-specific
tests: name a justified passing workflow subset and document its limits. Do not
reinterpret a short smoke test as full-project coverage. The receipt binds the
exact task seal, image, command, probe implementation, real tool completion and
underlying logs. Changes require fresh evidence. No real credentials or live
inference are used by this probe.

Create a private JSON control specification:

```json
{
  "schema": 1,
  "contract_review": "Explain supported inputs, observable invariants, valid alternative designs, and known limits.",
  "project_check": "python3 -m pytest -q tests/test_public_workflow.py",
  "controls": [
    {"id": "untouched", "role": "baseline", "source": "tasks/repair-case/environment/app", "must_fail": ["named-check"]},
    {"id": "reference", "role": "valid", "source": "controls/reference", "must_fail": []},
    {"id": "alternative", "role": "valid", "source": "controls/alternative", "must_fail": []},
    {"id": "partial", "role": "invalid", "source": "controls/partial", "must_fail": ["named-check"]}
  ]
}
```

Paths are relative to the JSON file. Replace the illustrative check IDs and add
invalid controls targeting every scoring bucket. The baseline must point to the
actual task's `environment/app`. At least two valid repairs with different source
bytes are required. Byte difference cannot prove independent reasoning: review
that the alternatives actually exercise different valid implementation choices.
Each defective control must retain a passing invariant and fail its declared checks, not merely crash or score
poorly for an unrelated reason. Dojo v6 additionally requires controls for
`different-skill-file` and `nonplugin-version-directory`.

```sh
obench repair validate /private/tasks/repair-case \
  --controls /private/controls.json \
  --workflow results/workflow-example/receipt.json \
  --image sha256:IMAGE --output /private/quality.json --json
```

A passing report means these particular controls and workflow passed. It does
not mean the prose is complete, every defect is covered, or the task is hard.
The gate records contract-review notes but cannot certify their truth. Review
behavioral coverage, alternate repairs and real input formats before authoring
controls; keep future confirmation tasks independent of this development set.

## Campaign admission and recovery

```sh
obench campaign launch suite.toml --admission /private/runtime/admission.json \
  --quality /private/task-one-quality.json \
  --quality /private/task-two-quality.json
```

Provide exactly one quality receipt per task. Launch checks task bindings,
image, implementation, control specification/source hashes and workflow evidence.
Admission reconstructs controls from the bound specification and replays each
through the confined grader; saved verdicts alone cannot authorize execution.
This requires Docker and adds control-replay time at launch and dispatch.
The supervisor repeats validation before execution and checks receipt hashes
against launch intent. Missing, failed or stale receipts block dispatch. Runtime
qualification retains its separate authentication/boundary controls. Direct
`obench run` remains the documented diagnostic/resume route; it does not issue
quality admission and must not be used to bypass campaign policy.

Exit codes for `repair`: **0** inspection/successful replay/passing controls;
**1** completed unsolved replay or failed controls; **2** invalid input or
incomplete execution/evidence. `--json` returns one structured object, including
incomplete errors. Help performs no grading. Original evidence is never rescored
in place; a new oracle requires a separately identified replay.

## Dojo v6 and development runtime v3

Dojo v6 uses standard `.../plugins/cache/<marketplace>/<plugin>/<version>/skills/`
locators and checks alias/version equivalence, multiplicity, replacement within
one plugin, marketplace changes, and version-like directories outside caches.
It also checks native output-text records and native-over-legacy precedence.
Use a new case revision when clarifying these input semantics in a prompt; retain
historical cases and oracle versions. Partial scores remain bucket-weighted;
inspect named checks rather than interpreting 0.667 as a test-pass fraction.

Runtime v3 adds pinned Tailwind/Svelte/Vite build dependencies, a Bash login shell,
and writable project-local `node_modules` scratch space. Packages remain immutable
in the offline image and resolve through `/node_modules`; no dependency links are
placed in the exported source tree. The synthetic Git baseline excludes generated
dependency scratch space. Rebuild by digest and renew admission after changes.

## Durable operation evidence

`repair replay` and `repair validate` with `--output FILE` also create a private
`FILE.evidence/` directory. Override it with `--evidence-dir DIRECTORY` (which
also works without `--output`). Each completed control is atomically published
before the next begins. The operation records stage timing, errors and a final
status. Persistence failures abort validation; unfinished attempts never pass.
A process killed without cleanup is reported as interrupted using its execution
lock, while completed controls remain available.

```sh
obench repair status /private/quality.json.evidence --json
```

This read-only command returns blockers, stage timing, and hashed evidence paths;
exit 2 means incomplete, interrupted or invalid evidence. Per-check worker
exceptions include bounded type, message and operation details. They are
untrusted candidate observations, not proof of an infrastructure failure.

Campaign launch retains fresh controls under `results_dir/admission-attempts/`;
failed attempts remain there even when no campaign launches. The supervisor
records its separate fresh replay under `campaign/dispatch-admission/`.
`campaign status` links both directories. Inspect the final result artifact for
the precise task, implementation, source and workflow identities used. A
recomputed passing report does not itself complete admission: saved-receipt
comparisons must also succeed before the operation is marked passed. These
records support diagnosis; they cannot replace authoritative replay on launch.

## Portable quality regression lane

Hosted Harbor CI runs `scripts/ci/verify_repair_quality.py` against real isolated
workers and the pinned Codex harness with a fake provider. Public synthetic
controls cover v6 polarity, two alternative identity implementations, each
scoring bucket, and the two source-identity defects. The lane proves an edited
failed receipt reaches replay and is rejected there; it also checks partial
validation evidence, an actual failed workflow and both worker error protocols.

Two complete runs use fresh containers and must agree on every named verdict.
Three fixed seeds vary plugin names, displayed names, multiplicity, versions and
non-plugin paths. Seeded invariants are diagnostic controls only; they do not
alter any oracle revision or historical score. The summary contains a
check-by-control matrix and all seeded outcomes. CI retains only this synthetic
summary, not harness transcripts. This coverage is a development test set, not
an independent benchmark holdout or evidence of model difficulty.
