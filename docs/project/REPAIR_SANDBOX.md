# Repair benchmark isolation

Status: implemented as an opt-in Harbor extension, 2026-09-16. Offline controls
and a bounded authenticated canonical control pass. **Longer repair-trial
evidence capture remains an admission gate before calibration.**

The native calibration exposed reference/checker files from another worktree;
those scores remain withdrawn as difficulty evidence. HOME isolation did not
restrict reads or SSH. The new lane enforces those boundaries in Docker.

## Execution boundary

```text
solver container (network=none, UID 10001)
  pinned Codex CLI -> loopback HTTP relay
                           |
                  per-trial Unix socket
                           |
trusted broker container -> fixed provider inference endpoint

broker + solver stopped -> bounded source snapshot -> fresh candidate worker
                                                            |
                                            host grader compares outputs
```

Harbor is the benchmark execution framework. Docker supplies the containers;
OpenBench supplies this environment, agent transport, and verifier extension.
Harbor retains trial scheduling, timeouts, artifacts, and cleanup.

The solver has no external network, host bind mounts, SSH agent, credentials,
Docker socket, or tailnet interface. It runs with a read-only root, no Linux
capabilities, no-new-privileges, and CPU/memory/process limits. Fresh named volumes
hold source and logs. A third volume exposes only the broker socket, read-only
to the solver. The relay contains no credentials. Bypassing or replacing it
still grants only the broker's restricted inference capability.

The runtime build context contains the pinned dependencies, Codex 0.154.0, and
public gateway/relay code. It contains no task, reference solution, grader,
repository history, or personal configuration. The trusted environment copies
only `environment/app/` into a fresh source volume before execution. Task-authored
Dockerfiles, compose overrides, arbitrary mounts, and network overrides are not
used by this lane.

### Model gateway

The broker accepts only the pinned Responses protocol at its inference route.
It sends requests to `https://chatgpt.com/backend-api/codex/responses`, with OAuth
staged only into the broker. It does not expose a generic proxy or CONNECT tunnel.
Redirects, alternate routes/models, hosted web search, remote MCP, file retrieval,
and URL-fetching input forms are rejected. Validation includes nested tool
namespaces and `input.additional_tools`, as emitted by Codex 0.154.0.
Provider-returned tool history may omit a namespace only when its short name
matches one declared local tool; unknown or ambiguous names remain rejected.

Request size, request count, concurrency, and duration are bounded. Disconnects,
timeouts, and trial shutdown cancel upstream work. A trusted metadata ledger
records requests, actual connected peers when available, usage, and clean shutdown;
it does not record prompts or credentials. Evidence persistence failures fail the
trial. The environment checks the runtime gateway's bytes against the reviewed
host module before reading credentials.
An occupied upstream slot returns retryable HTTP 503 without consuming request
budget; the total request limit remains HTTP 429. This lets a new tool turn
retry while the preceding stream finishes cleanup, without increasing concurrency.

The admitted model/effort pairs are Terra/xhigh and Luna/max, requested through
suite names `gpt-5.6-terra-xhigh` and `gpt-5.6-luna-max`. Existing stock treatments
remain unchanged. This custom HTTP transport is a **distinct treatment** until
live compatibility is established; do not pool it with native or older runs.
The broker does not refresh OAuth. Expired credentials fail closed.

The adapter also corrects a pinned Harbor 0.20.0 conversion bug: an identical
cumulative-and-last-usage snapshot can be reported again after partial model
output. It preserves that output without charging the same snapshot twice;
unchanged per-call counts with increased cumulative totals still count. State
resets per conversion, raw logs and reported cumulative totals remain unchanged,
and other inconsistencies still fail validation. Tracked upstream in
[Harbor #3289](https://github.com/harbor-framework/harbor/issues/3289).

The fixed provider endpoint can return a Responses event stream without a
`Content-Type` header. In that case the broker buffers at most 1 MiB and requires
a valid opening `response.created` SSE event before forwarding any bytes.
Explicit incompatible content types, malformed openings, and oversized prefixes
remain errors. The local metadata ledger includes the upstream HTTP status,
without recording provider error bodies or response headers.

### Trusted grading

The broker and solver containers must both be stopped, with PID zero, before
source extraction. The broker must confirm receipt drain. All solver descendants
are terminated; a background watcher cannot see later grading inputs.

Only the existing `scripts/profiles/*.py` files are submitted for the initial
`dojo-evidence-pr60-v3` task. Links, special files, missing files, excessive sizes,
and malformed archives are rejected without importing candidate code on the host.
Only source bytes enter a fresh network-disabled, unprivileged worker. The worker
receives test inputs and returns observations; the host retains the oracle and
computes the reward. Candidate output cannot directly supply a score.

Invalid source, malformed worker output, worker timeout, and candidate crashes
produce a trusted zero. Failure to establish or terminate the boundary remains
an infrastructure error. Invalid source has a failed workspace artifact plus a
trusted rejection receipt; the importer admits that specific zero instead of
excluding the attempt.

Scheme-3 task identity binds parsed task configuration, every task file (including
hidden files), the loaded grading module, and worker protocol. Suite identity also
binds the four implementation modules. Result import requires the task binding,
shutdown receipt, gateway ledger hash, worker restrictions, and matching frozen
source/image evidence. Ordinary digest schemes are unchanged. This lane is
currently local-only; public publication is rejected.

### Log export

Source archives retain their 2 MiB per-file and 16 MiB total limits. Agent logs
and declared log artifacts have a separate 16 MiB per-file and 48 MiB total
policy, inside the existing 64 MiB transfer and 4,096-entry bounds. Directory
and single-file downloads use the same log policy. Links, special files, path
escapes, duplicates, and malformed archives remain rejected before file writes.

Export failures leave a host-written, local-only JSON diagnostic under the
trial's `verifier/sandbox-exports/`. It records the requested path, archive size
when known, failure class, and violated size/limit when available; it contains
no transcript body or Docker stderr. A transfer-limit observation is a lower
bound, not a claim to know the entire rejected archive size. Rejected transcripts
are not copied outside those limits. Diagnostic persistence errors are explicit;
the environment also reports export failure at stop after cleaning up, because
pinned Harbor catches the original download exception. Missing required ATIF
evidence continues to block suite sealing.

## Build and verify

Build the immutable runtime, then use the returned `image_id`, not the mutable
tag. Build/probe receipts and raw logs belong in gitignored `results/`.

```sh
python3 scripts/local/build_repair_runtime.py \
  --receipt results/repair-runtime.json
python3 -m obench.sandbox_grading harbor-tasks-local/dojo-evidence-pr60-v3 --check
```

Run these scripts with the interpreter that has OpenBench and the pinned Harbor
installed. Use a named tmux session and persistent exit receipts for sustained
checks, following [benchmark operations](../benchmark-operations.md).

```sh
python scripts/local/verify_repair_sandbox.py \
  --runtime-image "$REPAIR_IMAGE" --receipt results/repair-boundary.json
python scripts/local/verify_repair_lifecycle.py \
  --runtime-image "$REPAIR_IMAGE" \
  --task harbor-tasks-local/dojo-evidence-pr60-v3 \
  --reference tasks-local/dojo-evidence-pr60/solution \
  --output results/repair-lifecycle
python scripts/local/verify_repair_log_export.py \
  --runtime-image "$REPAIR_IMAGE" \
  --task harbor-tasks-local/dojo-evidence-pr60-v3 \
  --output results/repair-log-export
python scripts/local/verify_repair_trajectory.py \
  --output results/repair-trajectory-control
OBENCH_GRADING_TEST_IMAGE="$REPAIR_IMAGE" \
  python -m unittest obench.tests.test_sandbox_grading -v
```

The lifecycle output directory must be new. These controls use synthetic
noncredentials and do not issue provider inference requests. The optional CLI
probe in `scripts/local/verify_repair_codex.py` uses an explicitly injected fake
provider; it exercises the installed Linux CLI, adapter, relay, and gateway
request protocol, not subscription authentication.

### Select the lane in a suite

Use the normal initialized project/suite layout. Put a copy of the sealed v3 task
in a dedicated local task-set directory containing only that task. Select stock
Codex arms with the admitted model names, `publication.scope = "local_only"`, and:

```toml
[sandbox]
kind = "repair-v1"
runtime_image = "sha256:<image_id from the build receipt>"
max_requests = 200
```

`obench run suite.toml --plan` validates and seals this intent without staging
auth or running models. Execution uses `obench run suite.toml --harbor-binary PATH`
and the existing stock OAuth staging lifecycle. Never use daily credentials for
an experiment without its authorization. The runtime image and matching editable
OpenBench installation must exist on the execution host; a stale gateway or
Harbor interpreter loading different implementation bytes is rejected.

This first version supports only the Dojo v3 task. Adding a new repair case
requires its own external behavioral oracle and submission contract; the current
Dojo oracle must not silently grade arbitrary tasks. Changes to task files or
grader code require a new scheme-3 seal and renewed controls.

## Evidence and remaining admission

Offline verification on the MacBook established:

- Known-reachable paired IPv4/IPv6 TCP 443, TCP 22, and UDP 53 controls succeed
  outside the solver and fail from it with network-unreachable errors.
- Direct, symlink, and subprocess attempts cannot read a known-present host
  canary; socket/capability checks deny ambient authority.
- A positive background watcher fires; a stopped watcher cannot mutate the
  subsequently extracted source.
- Actual Harbor trials score baseline 0, reference 1, and malicious symlink 0,
  without infrastructure exceptions. Partial and alternative repairs also pass
  their expected grading controls.
- Gateway socket/process tests cover policy denial, streaming, cancellation,
  quotas, receipt failures, connected-peer recording, and clean termination.

Authenticated verification on the MacBook at `d7322fe` established:

- Terra/xhigh and Luna/max completed a separately sealed file-edit control using
  the real OAuth endpoint and pinned Linux CLI. Exported source contained the
  requested edit, provider streams reached public TLS peers, both shutdown
  receipts were clean, and canonical suite import and run-manifest verification
  succeeded with transcript and usage evidence.
- This was an explicit negative grading control: its instruction requested only
  a harmless comment, so the unchanged repair checker correctly returned zero.
  It is not a failed repair attempt or a difficulty measurement.
- A separate full-repair diagnostic gave each model 600 seconds. Both timed out;
  the trusted artifacts scored Terra 1.0 and Luna 0.3333. Luna's agent-log export
  failed, leaving no converted ATIF trajectory. Required-evidence validation
  correctly refused to seal that suite. Those trials are not admitted calibration
  evidence, and a passing artifact is not a completed harness run.

The remaining capture investigation has a concrete reproduction: a 2 MiB + 1
byte agent-log file fails the same export route that accepts a small log. Log
export currently shares the source-file size limit. The original rejected Luna
logs were not retained, so this is a demonstrated limitation, not proof of that
attempt's exact cause. Separate bounded log/source policies and retained export
failure diagnostics are now implemented. The offline lifecycle probe verifies
3 MiB logs after completion and forced timeout, plus oversized-log and symlink
refusal. A fresh authenticated longer-run check remains required before
calibration.

A subsequent 600-second-per-arm diagnostic at `01cde29` captured both raw logs
and ATIF trajectories: Terra completed and Luna timed out. Suite import exposed
the repeated-usage conversion bug described above and correctly refused the
inconsistent trajectory. The correction passes both a synthetic reproduction
and read-only replay of both captured sessions. That rejected run remains
unchanged and is not admitted calibration; fresh canonical import is still
required after the correction.

These are control and diagnostic results, **not frontier-model difficulty
measurements**. Preserve the short control beside the longer-run failure, renew
controls when runtime bytes change, and verify sustained evidence capture before
screening and independently confirming harder cases. Previous native scores
remain excluded. Raw trial/provider artifacts stay local-only.

Residual limits: this does not defend against kernel/container-runtime exploits,
answers already in model weights, or a malicious provider. The broker is trusted
networked code. Source/log named volumes do not yet have aggregate disk quotas;
file/export, CPU, memory, process, and request limits are enforced. A disposable
worker VM with no personal repos or tailnet adds containment; moving to the Mini
alone does not. `tmux` survives disconnects, not laptop sleep or reboot.
