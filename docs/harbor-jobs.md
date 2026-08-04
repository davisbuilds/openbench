# Harbor-native jobs

`obench.harbor_job` authors deterministic native job configs for Harbor
`0.20.0`. It expands one task-set or pinned dataset across agent profiles,
models, and attempts. Harbor still owns task resolution, trial execution,
concurrency, retries, lock files, and resume. OpenBench does not add a
scheduler.

## Source pin

OpenBench's verified runtime and result importer pin Harbor commit
[`72bc40b1e58b47a9cc6e0f14c29aced3a9e53767`](https://github.com/harbor-framework/harbor/tree/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767),
whose package reports version `0.20.0`. This integration commit, rather than a
release-tag lookup, is the source of truth for this module. At that commit:

- [`pyproject.toml`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/pyproject.toml#L1-L4)
  declares version `0.20.0`.
- [`JobConfig`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/models/job/config.py#L344-L390)
  defines `n_attempts`, `n_concurrent_trials`, `retry`, `agents`, `datasets`,
  and `tasks`.
- [`DatasetConfig`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/models/job/config.py#L21-L46)
  accepts local `path` task sets or named dataset references and task filters.
- [`AgentConfig`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/models/trial/config.py#L61-L144)
  carries agent/import identity, model, per-agent concurrency, kwargs, env,
  and host allowlists.
- [`Job._init_trial_configs`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/job.py#L394-L421)
  performs attempts x tasks x agents expansion.
- [`harbor run -c`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/cli/jobs.py#L321-L335)
  accepts JSON or YAML implementing `JobConfig`.

## Run an OpenBench harness matrix

`obench harbor job-run` is the end-to-end local/Mini path. It exports the
selected tasks with public agent networking, resolves exact OAuth harness/model
profiles, holds one credential lease per harness, writes canonical job JSON
plus an OpenBench comparison-plan sidecar, and invokes one native
`harbor run -c` command:

```bash
obench harbor job-run \
  --tasks-dir tasks \
  --task make-it-run,fix-failing-test \
  --export-dir /absolute/run/exports \
  --harness codex \
  --harness pi \
  --model gpt-5.6-sol \
  --attempts 2 \
  --concurrency 2 \
  --max-retries 1 \
  --jobs-dir /absolute/run/jobs \
  --job-name openbench-harness-smoke \
  --config /absolute/run/openbench-harness-smoke.json
```

This example resolves to 2 tasks x 2 harnesses x 2 attempts = 8 trials.
The sidecar is written beside the config as
`openbench-harness-smoke.openbench-comparison-plan.json`. It binds the job
config SHA-256, exact task source, full rendered agent-config SHA-256 values,
canonical OpenBench harness/model labels, and attempt count. Local task names
are fixed before execution. For immutable registry/package datasets, the
sidecar fixes the exact name/version or package/ref descriptor and leaves
`tasks` null; the importer later binds Harbor's lock-resolved task set without
rewriting the canonical pre-run sidecar. Harbor still chooses execution order.
The sidecar proves intended and lock-resolved denominator identity, not temporal
matched scheduling, so `temporal_matched_block_claim` remains false.
Codex and Pi each have an independent OAuth lane, but each credential is used
by at most one trial at a time. A rerun with the identical config resumes the
same Harbor job. OpenBench does not replay completed trials itself.

Harbor's retry count and configured maximum are imported into every normalized
row as job-level provenance. `ApiUsageLimitError` is retryable when
`--max-retries` is nonzero; authentication, model identity, safety refusal,
timeout, and verifier-contract failures remain excluded from retry. Retried
attempts are Harbor infrastructure evidence and are not folded into a
successful trial's agent latency or token totals.

## Author a job

Local exports must point to the parent containing multiple Harbor task
directories, not to one task:

```python
from obench.harbor_job import (
    AgentProfile,
    ConcurrencyPolicy,
    HarborJobSpec,
    LocalTaskSet,
    RetryPolicy,
    build_command_plan,
    build_job_config,
    write_job_config,
)

spec = HarborJobSpec(
    job_name="openbench-candidate-20260803",
    jobs_dir="/absolute/path/to/harbor-jobs",
    source=LocalTaskSet("/absolute/path/to/harbor-export"),
    agent_profiles=(
        AgentProfile(
            profile_id="codex",
            arm_id="codex",
            canonical_harness="codex",
            canonical_model="gpt-5",
            name="codex",
            model_name="openai/gpt-5",
        ),
        AgentProfile(
            profile_id="candidate",
            arm_id="candidate-strict",
            canonical_harness="candidate",
            canonical_model="gpt-5",
            import_path="company.harbor:CandidateAgent",
            model_name="openai/gpt-5",
            kwargs={"mode": "strict"},
            n_concurrent=2,
            concurrency_group="company-api",
            env={"OPENAI_BASE_URL": "${OPENBENCH_PROXY_URL}"},
            extra_allowed_hosts=("proxy.internal",),
        ),
    ),
    models=(),
    attempts=3,
    concurrency=ConcurrencyPolicy(n_concurrent_trials=8),
    retry=RetryPolicy(max_retries=1),
)

artifact = build_job_config(spec)
config_path = write_job_config(artifact, "./harbor-job.json")
plan = build_command_plan(artifact, config_path)
assert plan.argv == ("harbor", "run", "-c", str(config_path))
```

For a published Harbor package dataset, use an immutable `ref`:

```python
from obench.harbor_job import Dataset

source = Dataset(
    name="openbench/core-smoke",
    ref="sha256:" + "0" * 64,
    task_names=("make-it-run",),
)
```

Legacy bare registry dataset names use `version` instead. Floating package
refs, unversioned registry datasets, source combinations, duplicate matrix
dimensions, partial local tasks, and unsafe paths fail closed.

## Config identity

`build_job_config` returns canonical UTF-8 JSON bytes and their SHA-256. Key
ordering, whitespace, task discovery, exception lists, and profile/model
expansion are deterministic. `write_job_config` creates the file atomically,
allows an identical existing file, and refuses replacement with different
bytes. Publish the JSON and `artifact.sha256` together; do not add OpenBench
metadata keys to the JSON because Harbor's native `JobConfig` is the contract.
`artifact.comparison_plan` contains the canonical create-once sidecar for both
local task sets and immutable datasets. Each arm may set `arm_id`,
`canonical_harness`, and `canonical_model`; its identity also includes every
secret-free rendered agent field, including kwargs, env templates, concurrency,
and host allowlists. Env values are never resolved into the plan. The resulting
task x arm x attempt matrix is a stable OpenBench denominator plan, not a second
scheduler.

The config fixes `job_name` and `jobs_dir`. Re-running the same
`harbor run -c <config>` points Harbor at the same job directory. Harbor then
compares its persisted `config.json` and resolved `lock.json`, preserves the
job ID, reconciles completed trials, and runs remaining trials. OpenBench's
command planner marks a job with persisted `config.json` as a resume candidate
but does not reinterpret partial state; Harbor owns that reconciliation.

Harbor's relevant behavior is source-backed by
[`Job._maybe_init_existing_job`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/job.py#L230-L250)
and
[`Job._write_job_lock`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/job.py#L878-L902).

## Integration contract

- The integrator installs and preflights the exact Harbor `0.20.0` binary.
- `obench.harbor_job` only authors the plan. `obench harbor job-run` is the
  integration layer that executes it.
- OAuth, auth-file mounts, token rotation, and counting-proxy lifecycle remain
  outside the job authoring module and inside the profile runner/agents.
- Runtime values enter only through `AgentProfile.env` Harbor templates of the
  form `${HOST_ENV}`. Literal values are rejected so configs remain safe to
  publish. The integrator populates those host variables before execution.
- `AgentProfile.arm_id`, `canonical_harness`, and `canonical_model` let a suite
  compiler name each comparison arm explicitly. `import_path`, `kwargs`, `env`,
  `extra_allowed_hosts`, `n_concurrent`, and `concurrency_group` remain the
  structured agent/auth/proxy fields.
- Sensitive-looking kwargs are rejected. Put credentials in env hooks, not in
  config fields.
- The integrator records `artifact.sha256`, `HARBOR_VERSION`, and
  `HARBOR_GIT_COMMIT` with run provenance and verifies Harbor result artifacts
  through the existing result-import boundary.

`artifact.trial_count` is exact for local exported task sets because task names
are enumerated before rendering. It is `None` for registry/package datasets;
Harbor resolves those task contents and records them in its lock file.

One comparison plan still covers one Harbor job and one task set. Grouping
multiple job plans into one suite comparison is a separate integration layer
and is intentionally not implemented here.
