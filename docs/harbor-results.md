# Import Harbor Results

`obench import harbor-results` converts a completed Harbor `0.20.0` job
directory into OpenBench `ROW_FIELDS` JSONL.

```bash
obench import harbor-results /path/to/harbor-job \
  --output results/harbor.jsonl
```

The importer is stdlib-only. It reads existing artifacts and never starts
Harbor, Docker, a model, or a verifier.

## Accepted evidence

The contract is intentionally narrow and fail-closed:

- Harbor version `0.20.0`, git commit
  `72bc40b1e58b47a9cc6e0f14c29aced3a9e53767`
- job lock schema `3` and trial lock schema `2`
- a finished top-level `result.json` whose completed counts, reward groups, and
  usage aggregates match the independently enumerated trial directories
- one directory for every resolved job-lock trial, with no extra directories
- single-step, exception-free trials
- each trial's `lock.json`, `result.json`, verifier reward, ATIF-v1.7
  `agent/trajectory.json`, `verifier/openbench-verifier-evidence.json`, and
  `artifacts/manifest.json`
- an explicitly requested artifact with destination `workspace`, a
  matching manifest entry with `type: "directory"` and `status: "ok"`, and a
  non-empty `artifacts/workspace/` directory

The importer cross-checks the job/trial lock multiset, the lock task digest,
Harbor's separate legacy task checksum, task/agent/model identity, phase timing,
scalar reward in `[0, 1]`, OpenBench verifier evidence, ATIF identity and usage
totals, job aggregates, artifact status, and duplicate identities.
The result-reported `agent_info.name` and ATIF agent name must also resolve from
the immutable trial-lock/config agent identity. Unlisted identities must match
exactly. The pinned OpenBench OAuth profile imports are explicit aliases for
`codex`, `pi`, and `opencode`. Any other custom identity that reports a
different semantic name is rejected.

Codex-profile and Pi-profile trials additionally require
`agent/harbor-metering/`: one public evidence JSON plus one private durable
ledger and seal. The importer recomputes the ledger chain, checks the trial and
harness identity, and reconciles calls/input/cache/output with ATIF.

Any missing, partial, random, duplicate, unresolved, or contradictory evidence
rejects the whole job before the output file is opened for append.

## Row semantics

Trials are grouped by `(task, agent, model)`, sorted by Harbor trial name and
ID, and numbered from one within each group. This is deterministic bookkeeping;
it does not claim that trials are temporal matched blocks.

- `exec_mode` is `harbor`.
- Harbor's `agent_result` usage is labeled `harbor_agent_reported`.
- Required metered profiles also receive `proxy_measured` fields. Exact
  reconciliation is labeled `Harbor-reported + proxy-verified`. A structurally
  valid mismatch preserves both lanes and is excluded from usage rankings.
  Missing, incomplete, malformed, unsealed, or tampered metering rejects the
  job.
- `success` is strictly `checker_exit == 0`, matching `obench.run`; a nonzero
  exit remains unsuccessful even when its parsed and clamped score is `1.0`.
- `score` and `checker_exit` come from the exporter's
  `openbench-verifier-evidence-v2` record after matching it to Harbor's reward
  file and trial result. `t_checker_s` uses that record's checker duration;
  Harbor's broader verifier phase duration remains separate in provenance.
  Checker stdout/stderr remain null.
- Harbor must report the pinned `0.20.0` commit as a non-editable install.
  Editable installs are rejected because their source cannot be immutably
  identified by the package version and commit fields alone.
- `candidate_provenance` records Harbor build, job/trial IDs, evidence SHA-256
  digests, both Harbor task hashes, the structured OpenBench scheme-2 task
  content digest, structured exporter parameters, workspace tree digest, usage
  source, job retry count/configured maximum, and mapping semantics.

## Publishing imported rows

`obench publish` treats `exec_mode = "harbor"` as a strict trust boundary. The
complete normalized `candidate_provenance`, `workspace_source`, and usage
contract must be present and internally consistent. Partial Harbor provenance,
unknown provenance keys, invalid digests, workspace-digest disagreement, proxy
claims, semantic harness/config identity disagreement, or usage-total
disagreement fail before a bundle is written.
For metered profiles, exact equality supports the `proxy-verified` label.
Structurally valid mismatches remain publishable with both values and a visible
warning, but cannot contribute to token, cost, or efficiency rankings.
Correctness and latency remain independently publishable. Non-proxy profiles
publish available ATIF usage as `Harbor-reported`.
For every Harbor task, the imported scheme-2 execution digest must equal the
digest recomputed from the local publication task tree. Missing local task
trees, inconsistent imported digests, or any mismatch fail before the output
directory is created.
Publication also deterministically re-exports that local task with the
evidence-bound `base_image` and `network_mode`, reproduces Harbor 0.20.0's
package content hash, and requires equality with `TrialLock.task.digest`. This
rejects exports changed after the OpenBench digest was embedded.
Canonical publication is deliberately non-executing: git workspaces with
`setup` hooks and remote git workspaces are rejected instead of running task
code or fetching network content during publish or verify.

Published Harbor rows use an allowlist. They retain the normalized result
metrics, Harbor/ATIF/verifier/artifact/final-workspace digests, task hashes,
mapping semantics, and Harbor-reported usage provenance needed to support the
claim. Raw trajectory/session/transcript/workspace paths, unrecognized fields,
and credential material are never copied. `provenance.json` records the same
safe evidence per run under `harbor_import_evidence`; `obench verify`
recomputes that manifest from `results.jsonl`, checks the executed scheme-2
digest against the per-task publication digest, reproduces the locked Harbor
task digest, and then recomputes the digest from the supplied local task tree.

## Append safety

Before appending, the importer validates the entire job and scans every
existing non-empty JSONL line. Corrupt JSONL, duplicate existing IDs, or a
collision with an imported `run_id` rejects the operation without mutation.
Accepted rows use `ROW_FIELDS` order. The collision scan and full-batch append
run under one exclusive file lock; the batch is flushed and `fsync`ed once, and
the output is truncated back to its prior length if append fails.

Multi-step and exception-bearing Harbor trials are rejected rather than
partially interpreted. Supporting them requires a separate contract for
step-level reward, trajectory, artifact, and failure evidence.
