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
completeness = "complete"
```

Every shown key is required. Unknown keys fail closed at the root and in every
nested table.

## Task sets

A suite has one or more task sets.

`kind = "local"` requires `path` and rejects external-source fields. The path:

- is relative to the project root;
- cannot be the project root, absolute, home-relative, or contain `..`;
- cannot traverse a symlink;
- must contain at least one child with `task.toml` and `instruction.md`;
- rejects partial Harbor task children and symlink children.

`kind = "harbor"` requires `name` and immutable `ref`, and rejects `path`.
Accepted refs are exact semantic versions, full 40/64-hex commits, and
`sha256:<64 hex>`. Optional `git_commit` records exact source provenance;
optional `subdir` requires that commit.

Task-set IDs and source identities must be unique.

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

Publication completeness is either:

- `complete`: every intended task x arm x attempt cell must have acceptable
  evidence before publication.
- `allow_incomplete`: an explicitly partial artifact may be produced by future
  publication integration.

The suite records policy only. Current run, result, report, and publication
modules do not consume it yet.

## Validate

```python
from obench.suite import load_suite

suite = load_suite(".openbench/suites/default.toml")
print(suite.id, suite.harbor.version, len(suite.arms))
```

The conventional `.openbench/suites/` location lets the parser discover the
project root. Call `load_suite(path, project_root=...)` for a suite stored
elsewhere.
