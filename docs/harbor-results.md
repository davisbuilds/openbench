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
  `459ff6ec99417589b7f679d14ddf3b3f0ae4f1dc`
- job lock schema `2` and trial lock schema `1`
- a finished top-level `result.json` whose completed count and embedded trial
  results match the independently enumerated trial directories
- one directory for every resolved job-lock trial, with no extra directories
- single-step, exception-free trials
- each trial's `lock.json`, `result.json`, verifier reward, ATIF-v1.7
  `agent/trajectory.json`, and `artifacts/manifest.json`
- an explicitly requested artifact with destination `final-workspace`, a
  matching manifest entry with `type: "directory"` and `status: "ok"`, and a
  non-empty `artifacts/final-workspace/` directory

The importer cross-checks the job/trial lock multiset, task digest and result
checksum, agent and model identity, phase timing, scalar reward in `[0, 1]`,
ATIF identity and usage totals, aggregate trial copies, artifact status, and
duplicate identities.

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
- A scalar reward of `1.0` maps to `success: true` and `failure_class: solved`.
  Lower rewards retain their score and map to `wrong_answer`.
- `checker_exit`, `checker_stdout`, and `checker_stderr` remain null. Harbor
  reward evidence is not rewritten as an OpenBench checker execution.
- `candidate_provenance` records Harbor build, job/trial IDs, evidence SHA-256
  digests, task digest, final-workspace tree digest, usage source, and mapping
  semantics.

## Append safety

Before appending, the importer validates the entire job and scans every
existing non-empty JSONL line. Corrupt JSONL, duplicate existing IDs, or a
collision with an imported `run_id` rejects the operation without mutation.
Accepted rows use the runner's ordered append helper, including flush and
`fsync` after each row.

Multi-step and exception-bearing Harbor trials are rejected rather than
partially interpreted. Supporting them requires a separate contract for
step-level reward, trajectory, artifact, and failure evidence.
