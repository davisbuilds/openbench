# Harbor Codex OAuth

OpenBench can run one exported Harbor task with Codex OAuth while preserving a
rotated `auth.json` on the host. The bridge is optional: importing
`obench.harbor_run` does not import Harbor, and the custom agent reports a clear
setup error if Harbor is not installed.

Run one exported task with explicit inputs:

```bash
obench harbor oauth-run \
  --task /absolute/path/to/exported-task \
  --model openai/gpt-5 \
  --master-auth-json /absolute/path/to/auth.json \
  --jobs-dir /absolute/path/to/harbor-jobs \
  --job-name oauth-smoke-001
```

Use `--harbor-binary /absolute/path/to/harbor` when the required Harbor binary
is not available as `harbor` on `PATH`.

On completion, the command reports Harbor's exit code and the expected
`jobs_dir/job_name` output path. It returns Harbor's exit code unchanged,
including nonzero codes. If credential finalization fails after Harbor exits,
the command still reports the exact Harbor code and propagates it when nonzero;
a Harbor `0` with failed credential finalization returns `2` so the overall
operation cannot appear successful. Argument, preflight, and OAuth preparation
errors also return `2` with a concise `ERROR:` message. The CLI never prints
credential contents, the staged credential paths, or the constructed Harbor
command.

The same runner remains available as a stdlib API:

```python
from obench.harbor_run import run_harbor_oauth

result = run_harbor_oauth(
    task_dir="/absolute/path/to/exported-task",
    model="openai/gpt-5",
    master_auth_json="/absolute/path/to/auth.json",
    jobs_dir="/absolute/path/to/harbor-jobs",
    job_name="oauth-smoke-001",
)

print(result.returncode)
print(result.expected_job_path)
```

All inputs are explicit. `task_dir` must be one task from the OpenBench Harbor
1.4 exporter: its direct `task.toml` must declare `schema_version = "1.4"`,
`[metadata].origin = "openbench"`, and exactly one artifact from `/app` to
`workspace`. Legacy, foreign, altered, or dataset-style task roots are rejected.
`job_name` is required so the expected output is always `jobs_dir/job_name`.
Existing job paths and symlinked jobs directories are rejected rather than
implicitly resumed or redirected.

## Fixed Harbor Contract

Before reading or staging `auth.json`, the runner resolves `harbor --version`
and requires exactly Harbor `0.20.0`. It then builds the equivalent of:

```text
harbor run \
  -p /absolute/task \
  -a obench.harbor_agents.codex:OpenBenchCodexOAuth \
  -m MODEL \
  -k 1 \
  -n 1 \
  -r 0 \
  -o /absolute/jobs \
  --job-name NAME \
  --ae CODEX_AUTH_JSON_PATH=/private/stage/auth.json \
  --ae OPENBENCH_CODEX_AUTH_RETURN_PATH=/private/stage/auth-return.json
```

The two `--ae` values are paths. Credential bytes are never placed in argv,
the child environment, or the returned plan. The temporary directory is mode
`0700`; the staged and returned files are mode `0600`.

The Harbor process exit code is returned unchanged after credential
persist-back. A nonzero Harbor exit can therefore still preserve a valid
rotation. Missing return, changed upstream cleanup behavior, account identity
changes, concurrent use, and stale copy-and-swap generations all fail closed.
Staging cleanup runs on success and failure.

The version preflight proves the Harbor package version, not its source commit.
Post-run evidence must remain unaccepted until the Harbor-results import gate
confirms commit `72bc40b1e58b47a9cc6e0f14c29aced3a9e53767`.

## Why One Trial

This runner intentionally fixes attempts and concurrency to one and Harbor
retries to zero. A `HarborOAuthCredential` context stages one credential
generation and receives one rotated generation. Multiple trials inside that
context would all start from the same copy, so their rotations cannot be
safely ordered or chained. Run trials sequentially instead: after the first
context persists its returned rotation, the next invocation stages that newer
master.

The master credential uses the same mode-`0600` per-file lease as native
OpenBench Codex runs, Codex ablations, and container runs. The lease spans
generation snapshot, staging, execution, and persist-back. Harbor acquires it
nonblocking and fails if another consumer owns the credential; native runs wait.
Compare-and-swap validation immediately before atomic replacement also rejects
a generation changed by a non-cooperating writer.
