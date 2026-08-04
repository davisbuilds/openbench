# Harbor-native suite files

`suite.toml` is the human-authored source of benchmark intent. It selects
immutable task sets, harness/profile/model arms, run policy, required evidence,
and publication completeness. It does not contain generated Harbor job config,
runtime state, or credentials.

`obench.suite` is a standard-library-only parser and immutable data model. It
does not import Harbor or execute a benchmark.

## Version 1

```toml
schema_version = 1
id = "private-default"
title = "Private repository benchmark"

[harbor]
version = "0.20.0"
commit = "72bc40b1e58b47a9cc6e0f14c29aced3a9e53767"

[[task_sets]]
id = "private"
kind = "local"
path = ".openbench/tasks"

[[arms]]
id = "codex-example"
harness = "codex"
profile = "local-codex"
model = "gpt-5.6-sol"

[run]
attempts = 1
concurrency = 1
max_retries = 0
timeout_seconds = 900

[evidence]
harbor_lock = true
verifier = true
trajectory = true
usage = true

[publication]
scope = "local_only"
completeness = "complete"
```

Every shown key is required. Unknown keys fail closed at the root and in every
nested table.

## Task sets

A suite has one or more task sets.

`kind = "local"` requires `path` and rejects external-source fields. The path:

- is relative to the project root;
- cannot be the project root, absolute, home-relative, or contain `..`;
- cannot contain or traverse a symlink anywhere in the task-set tree;
- must contain at least one child with `task.toml` and `instruction.md`;
- rejects partial Harbor task children and symlink children.

`kind = "harbor"` requires `name` and immutable `ref`, and rejects `path`.
Accepted refs are exact semantic versions, full 40/64-hex commits, and
`sha256:<64 hex>`. Optional `git_commit` records exact source provenance;
optional `subdir` requires that commit.

Task-set IDs and source identities must be unique. Bare registry names map
`ref` to Harbor's exact `version` field and therefore require a semantic
version. Names containing `/` map `ref` to Harbor's immutable package `ref`.
Hexadecimal commits and digests are canonicalized to lowercase before source
identity comparisons.

## Arms and run policy

Each arm requires an `id`, `harness`, `profile`, and `model`. IDs and complete
`(harness, profile, model)` tuples must be unique. `profile` is an identifier,
not a filesystem or credential path.

`attempts` and `concurrency` are positive integers. `max_retries` is a
non-negative integer. `timeout_seconds` is a positive finite number. TOML
booleans are never accepted as integers, and `nan`/`inf` are rejected.

## Evidence and publication

The four evidence fields are explicit booleans:

- `harbor_lock`: require Harbor's resolved task/runtime lock.
- `verifier`: require verifier output and reward evidence.
- `trajectory`: require the agent trajectory.
- `usage`: require usage evidence under the selected profile policy.

Publication scope is explicit:

- `local_only`: artifacts must remain machine-local.
- `public`: publication is permitted after the evidence and completeness gates.

Publication completeness is either:

- `complete`: every intended task x arm x attempt cell must have acceptable
  evidence before publication.
- `allow_incomplete`: an explicitly partial artifact may be produced by future
  local measured output that is never public.

`scope = "public"` requires `completeness = "complete"`. Smoke suites and
`local_only` suites cannot be published, accepted by community sync, or
ingested by the site.

## Plan

From any directory under a configured project, the suite path may be omitted:

```bash
obench run --plan
obench run .openbench/suites/alternate.toml --plan
```

The omitted form discovers the nearest `.openbench/openbench.toml` and requires
its `default_suite`. An explicit suite path overrides only that selection.
`--plan` validates suite/profile/model compatibility and native Harbor job
compilation, then prints a canonical semantic manifest and SHA-256. It does not
inspect Harbor, stage credentials, invoke a model, or create runtime artifacts.

The manifest records:

- the exact Harbor pin and each task-set source identity;
- content digests and logical task names for local task sets;
- canonical OpenBench harness/model identity per arm;
- the full secret-free rendered Harbor agent config and its digest;
- run, evidence, and publication policy;
- one declared Harbor job per task set.

Absolute paths, host credential paths, and credential contents are excluded.
Relocating an otherwise identical project preserves the manifest digest.

## Execute

```bash
obench run
obench run .openbench/suites/alternate.toml
```

OpenBench enforces the suite pin against its Harbor `0.20.0` constants before
auth staging. It emits one native `harbor run -c` per task set. Harbor alone
owns scheduling, task resolution, retries, locks, resume, Docker environments,
and verification. OpenBench does not add a scheduler or claim temporally
matched arm execution.

Each arm is compiled independently and uses its arm ID as execution identity.
Stock profiles reject harness mismatches and unsupported models. One stock
profile used by several model arms gets one staged credential/lease for the
suite execution; rotation is persisted once after all Harbor commands.
Read-only Cursor and Devin subscription archives remain read-only.

`run.timeout_seconds` maps to pinned Harbor
`AgentConfig.override_timeout_sec` on every generated agent config. A Harbor
schema without that field must be rejected rather than silently ignoring the
policy.

Custom profiles resolve only their declared `${HOST_ENV}` templates, and
missing values fail before Harbor. Their required `distribution` and exact
`version` are verified through `importlib.metadata`, including ownership of the
declared import's top-level package, before Harbor or stock auth staging.

The immutable semantic manifest is written under
`.openbench/results/suite-runs/`; exact native configs and canonical comparison
plan sidecars stay under `.openbench/jobs/suite-configs/`; Harbor job state
stays under `.openbench/jobs/`.

After every Harbor command succeeds, OpenBench imports every intended job,
enforces the declared task x arm x attempt denominator and evidence policy, and
writes one suite results JSONL atomically. A failure in any job, import, binding,
or evidence check leaves no partial results file. Exact existing results and
run-manifest bytes are accepted on resume; divergence fails.

Each row embeds the canonical secret-free suite semantic manifest and digest,
task-set identity, publication scope/completeness, and its exact per-job
comparison plan. Multi-task-set matched comparisons include suite, task-set,
plan, task, and attempt identity. They never claim temporal scheduling;
`temporal_matched_block_claim` remains false.

The sealed public run manifest binds the suite semantic body/digest, each
task-set config path identity and SHA-256, comparison-plan body/digest, expected
Harbor job name, and final result SHA-256/count. Absolute runtime paths live
only in the separate local record. The CLI prints all paths.

Verify a local or public suite seal without executing Harbor:

```bash
obench run --verify-run-manifest \
  .openbench/results/suite-runs/<digest>.run.json
```

The old OpenBench cell runner is available only through:

```bash
obench legacy run ...
```
