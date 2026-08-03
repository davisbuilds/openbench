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

Any missing, partial, random, duplicate, unresolved, or contradictory evidence
rejects the whole job before the output file is opened for append.

## Row semantics

Trials are grouped by `(task, agent, model)`, sorted by Harbor trial name and
ID, and numbered from one within each group. This is deterministic bookkeeping;
it does not claim that trials are temporal matched blocks.

- `exec_mode` is `harbor`.
- Harbor's `agent_result` usage is labeled `harbor_agent_reported`.
- All proxy token fields remain null and provenance records
  `proxy_measured: false`.
- `success` is strictly `checker_exit == 0`, matching `obench.run`; a nonzero
  exit remains unsuccessful even when its parsed and clamped score is `1.0`.
- `score` and `checker_exit` come from the exporter's
  `openbench-verifier-evidence-v1` record after matching it to Harbor's reward
  file and trial result. `t_checker_s` uses that record's checker duration;
  Harbor's broader verifier phase duration remains separate in provenance.
  Checker stdout/stderr remain null.
- `candidate_provenance` records Harbor build, job/trial IDs, evidence SHA-256
  digests, both Harbor task hashes, workspace tree digest, usage source, and mapping
  semantics.

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
