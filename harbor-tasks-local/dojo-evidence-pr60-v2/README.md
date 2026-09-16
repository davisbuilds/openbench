# Dojo evidence repair, isolated v2 candidate

Fork-local Harbor-format package derived from
[`tasks-local/dojo-evidence-pr60`](../../tasks-local/dojo-evidence-pr60/PROVENANCE.md).
The original task and its results remain unchanged. This package is an offline
admission candidate, **not a calibrated extra-hard task or a live-ready suite**.

## V2 contract correction

The original verifier required literal `origin:name` strings in mismatch
diagnostics, although the instruction only promised detection, multiplicity,
and the existing return shape. V2 accepts any nonempty string labels in those
lists. It still requires correct live/recorded entry counts, directional
difference counts, duplicate multiplicity, origin-swap detection, and the
positive alias/plugin-version equivalence case. It does not accept missing
differences or collapse duplicate entries.

Workspace, instruction, checker orchestration, test inventory, and reference
solution bytes are unchanged. Only three incidental display assertions in
`tests/checker_data/oracle.py` are relaxed; the duplicate case also checks that
the opposite directional difference remains empty. The package version and
contract digest are new. The scheme-2 digest is computed from the original
native task with this v2 oracle substituted, not from the original oracle.

## Execution boundary

- Docker build context is **`environment/`**, not the repository or task root.
- The image contains the source workspace and PyYAML **6.0.3**, installed on the
  digest-pinned Python 3.12 Debian base in `environment/Dockerfile`.
- No solution, checker, source Git history, previous attempt, host checkout, or
  daily harness configuration is mounted or copied into the agent image.
- `tests/` is a separate verifier payload, injected **after** the agent phase.
  `solution/` is for a separate oracle trial only.
- The checked-in task policy is `no-network`. The offline probe enforces this
  with Docker's `--network none`, no mounts, dropped capabilities, and
  `no-new-privileges`.

The image is a filesystem boundary, not protection against arbitrary hostile
verifier-time candidate code. The verifier imports candidate modules in the
same container; stronger tamper resistance needs separate verifier execution.

## Reproduce offline admission

From the repository root, with Docker running:

```bash
python3 scripts/local/verify_dojo_container.py \
  --receipt results/dojo-container-admission-v2/receipt.json
```

Image build may download the pinned base/dependency. Every probe and checker
executes offline, with no model calls or credentials. Use `--skip-build` only
when deliberately reusing the image; the receipt records its immutable ID.
Containers and temporary canaries are removed; the image and requested receipt
remain. This command is a short offline validation, not a multi-trial campaign.

The probe checks:

1. The allowed workspace read/write succeeds, including a child process. The
   host independently copies back the resulting file to verify the write.
2. Direct, symlink, and subprocess reads of a real host-only canary fail. The
   known original host solution path is also unreadable. Docker inspection
   confirms no mounts. The host checks its canary was not changed.
3. Deliberately copying that same canary into a control container makes the
   exact denial probe fail, so absence is not inferred from a broken detector.
4. Buggy workspace scores **0**, reference scores **1**, and all six partial
   repairs score the expected **1/3** or **2/3**.
5. A valid alternate implementation retains qualified multiset comparison but
   renders bare diagnostic names. It fails the old verifier's format assertion
   (**2/3**) and passes v2 (**1**).

The receipt fingerprints the image and package files and records checker
output, rewards, runtime identity, and each capability probe. It also verifies
that the original native task was unchanged.

## Remaining admission work

An additional offline run through Harbor 0.20.0 at the repository's pinned
commit completed the reference agent with reward 1, no errors and no retries
on 2026-09-16. The local receipt is
`results/extra-hard-preparation/harbor-jobs/dojo-v2-offline-boundary/`.
This establishes task-schema/build/verifier compatibility on the tested Docker
runtime. It does not establish the authenticated model path or its egress policy.

This proves the explicit Docker launch in the probe. It does **not** prove
Harbor's authenticated production launch, verifier lifecycle, API networking,
or candidate configuration. Repeat the capability probe through that actual
entry point before any model calibration; do not substitute host `workspace-write`
for container confinement. Public-source retrieval remains a separate
contamination risk if API networking also permits arbitrary Internet access.

This dependency-installing Dockerfile is intentionally different from the
stock `export_task` template. The legacy export publication path reconstructs
that stock Dockerfile, so do not claim this customized package is publishable
through that route. One reproducible bridge is to build a dependency-only image
(no task workspace or oracle), pin its image digest, create a versioned native
v2 task, and run the standard exporter with that base image. Validate the
resulting suite/task/evidence bindings before running or publishing.

Do not pool v1 host-run results with this new verifier/environment cohort.
