# Benchmark versioning

Isolated repair benchmarks track **case revision**, **oracle revision**, and
**execution treatment** separately. Use positive integers for the first two;
there is no automatic increment on commits or PRs. Exact SHA256 seals remain
the authority for reproducing a run. Revisions explain what changed.

## What advances

| Change | Action |
| --- | --- |
| Prompt, buggy source, supplied dependencies, build inputs, or intended repair contract | Advance the case revision. Also advance the oracle if its behavior changes. |
| Hidden examples, observer queries, acceptance rules, score calculation, or success criteria | Advance the oracle revision. Keep the case revision when the model's problem is unchanged. |
| Model, reasoning effort, harness/adapter, runtime image, timeout, request budget, concurrency, retries | Change the execution treatment in the suite. Keep case/oracle revisions unless their semantics also change. Renew runtime admission when required. |
| README/provenance edits, refactors with verified identical grading behavior, resealing shared implementation dependencies | Keep semantic revisions; refresh the exact seals. Document the reason when it affects interpretation. |

A build/runtime change can affect more than one column: editing the task's
Dockerfile changes its case inputs, while selecting a different solver runtime
image changes the execution treatment. Neither can silently reuse the previous
suite identity.

## Task declaration

New repair packages use `<case>-c<case_revision>-o<oracle_revision>` for their
directory, `metadata.openbench_task`, and the suffix of `[task].name`:

```toml
[task]
name = "openbench/dojo-evidence-pr60-c2-o5"
version = "1.0.0"

[metadata]
openbench_task = "dojo-evidence-pr60-c2-o5"

[metadata.openbench_revision]
case = "dojo-evidence-pr60"
case_revision = 2
oracle = "dojo-evidence"
oracle_revision = 5
```

The example is an identity fragment; retain the package's other required Harbor
fields and reseal the complete task. AgentMonitor additionally declares
`metadata.openbench_oracle = "agentmonitor-benchmark-v3"` to select its registered
worker protocol. That selector must agree with the explicit oracle revision.
The trusted registry remains closed: task metadata cannot import arbitrary code.

Harbor's `[task].version` remains a package-format field, not the benchmark's
semantic counter. Existing values stay intact; a newly named package can start
at `1.0.0`. Installed task-pack versions, OpenBench's package version, Harbor
`schema_version`, and digest scheme numbers retain their separate meanings.

The compiler validates the declaration and records `repair_revision` in each
repair task set in the sealed suite manifest. It contains the four fields above
plus `case_sha256`, a canonical fingerprint of `instruction.md` and every file
under `environment/`, including hidden build files. Docs, hidden tests and task
metadata remain covered by the full task seal rather than this component hash.
Model/effort, runtime and budgets are already bound in the same suite manifest;
they do not need another manually incremented execution counter.

Revision numbers are a reviewed semantic declaration, not automatic proof that
two cases are equivalent. Compare the case fingerprint as well. Changed bytes
under an unchanged case number are a signal to inspect the revision decision;
the full suite seals still distinguish those runs. Qualification's bounded
file-edit controls intentionally have different prompt hashes and live under
their separate control suite identity.

## Historical aliases

Existing task directories stay in place. Their names are permanent aliases:

| Historical task | Case | Case revision | Oracle | Oracle revision |
| --- | --- | --- | --- | --- |
| `dojo-evidence-pr60-v3` | `dojo-evidence-pr60` | 1 | `dojo-evidence` | 3 |
| `dojo-evidence-pr60-v4` | `dojo-evidence-pr60` | 2 | `dojo-evidence` | 4 |
| `dojo-evidence-pr60-v5` | `dojo-evidence-pr60` | 2 | `dojo-evidence` | 5 |
| `am-benchmark-pr106-v3` | `am-benchmark-pr106` | 1 | `agentmonitor-benchmark` | 2 |
| `am-benchmark-pr106-v4` | `am-benchmark-pr106` | 1 | `agentmonitor-benchmark` | 3 |

Dojo v4 clarified the prompt, so it starts case 2. V5 only corrected unsupported
budget observations. The two AgentMonitor packages share prompt and source;
oracle 3 removed an unintended storage-ID requirement. Earlier non-isolated
prototypes are outside this mapping and retain their original identities.

`obench/repair_identity.py` owns the alias map and validates explicit declarations.
Aliases cannot be rebound to another case/oracle pair. New manifests include the
structured fields even for legacy task names. Historical manifests without
these fields remain readable; do not retrofit or overwrite published/sealed
results. Reproducing an old execution uses its original commit and seals.

## Making a change

1. Identify which row of the change table applies. State the before/after
   behavior in the PR; don't bump numbers solely because a file changed.
2. For a new semantic pair, use a new canonical task name and explicit metadata.
   Keep historical packages and oracle behavior available. Register new oracle
   revisions deliberately in the trusted code.
3. Exercise positive and negative controls. An oracle correction must accept
   the newly permitted valid repair while still rejecting actual defects.
4. Refresh task bindings from their manifest, then compile the suite. Check its
   case fingerprint, oracle revision and execution settings before qualifying.
   Shared implementation edits currently refresh sibling task seals too; this
   is a byte dependency change, not a reason to increment their semantic numbers.
5. Preserve original scores. Score an existing frozen artifact under a corrected
   oracle as a separately identified **replay**. Fresh model attempts under that
   oracle are another dataset; neither inherits old difficulty claims.

Do not pool results merely because their case revision matches. Case inputs,
oracle and execution treatment must be comparable for the measurement, with
any cross-version comparison made explicit. Existing full-suite matching stays
conservative; the new fields do not relax comparison or publication checks.
