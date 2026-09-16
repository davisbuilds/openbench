# Repair benchmark isolation

Status: implemented as an opt-in Harbor extension, 2026-09-16. Offline runtime
and trial controls pass. **Authenticated provider transport and a complete live
`obench run` still require admission before calibration.**

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

Request size, request count, concurrency, and duration are bounded. Disconnects,
timeouts, and trial shutdown cancel upstream work. A trusted metadata ledger
records requests, actual connected peers when available, usage, and clean shutdown;
it does not record prompts or credentials. Evidence persistence failures fail the
trial. The environment checks the runtime gateway's bytes against the reviewed
host module before reading credentials.

The admitted model/effort pairs are Terra/xhigh and Luna/max, requested through
suite names `gpt-5.6-terra-xhigh` and `gpt-5.6-luna-max`. Existing stock treatments
remain unchanged. This custom HTTP transport is a **distinct treatment** until
live compatibility is established; do not pool it with native or older runs.
The broker does not refresh OAuth. Expired credentials fail closed.

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

These are control results, **not frontier-model difficulty measurements**. Before
another campaign, run one bounded authenticated canonical suite with an approved
benchmark credential, inspect its real provider peer and stream, and verify the
atomic imported suite result. Then screen and independently confirm harder cases.
The previous native scores remain excluded.

Residual limits: this does not defend against kernel/container-runtime exploits,
answers already in model weights, or a malicious provider. The broker is trusted
networked code. Source/log named volumes do not yet have aggregate disk quotas;
file/export, CPU, memory, process, and request limits are enforced. A disposable
worker VM with no personal repos or tailnet adds containment; moving to the Mini
alone does not. `tmux` survives disconnects, not laptop sleep or reboot.
