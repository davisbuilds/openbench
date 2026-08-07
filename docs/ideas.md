# OpenBench ideas

This document records product and architecture ideas, including decisions that
superseded earlier options.

## Consolidate benchmark execution

### Motivation

The historical runner accumulated several overlapping paths:

- local agent plus host checker;
- generic OpenBench agent image plus host checker;
- generic agent image plus a checker-owned image;
- task-specific image used for both agent and verifier;
- legacy imported tasks with hardcoded checker image tags.

This makes task behavior harder to explain, validate, and reproduce.

### Decision: Harbor for coding tasks

Coding-task execution now uses one canonical contract:

1. Every task resolves one versioned task manifest.
2. The manifest selects one pinned task environment and verifier contract.
3. The agent runs in that environment with a disposable writable workspace.
4. The verifier runs afterward in a fresh container using the same environment.
5. Hidden checker assets mount read-only only during verification.
6. OAuth/API credentials are staged at runtime into a disposable container home;
   secrets are never baked into images or artifacts.
7. Simple tasks use a shared minimal task image rather than host execution.
8. Legacy checker-hardcoded images and silent Docker-to-local fallback are
   removed after migration.

### Decision record

OpenBench compiles suites into Harbor jobs. Harbor owns task containers,
concurrency, retries, resume, verifier execution, and trial artifacts. OpenBench
owns comparison, evidence policy, and publication. This minimizes duplicated
infrastructure while retaining OpenBench's harness identity, metering,
comparison, and publication contracts.

The historical OpenBench runner remains compatibility-only. It is not a second
canonical execution backend.

### Native platform exception

Some benchmark families require a host platform Harbor cannot provide. The
first explicit exception is Computer-Use Bench on macOS:

- `exec_mode = "native_macos"` is a distinct, named backend, never a silent
  Docker fallback.
- It uses a whole-run machine lease, source-proven preflight, deterministic
  setup/reset, focus and lock monitoring, MCP collection, ATIF, verifier
  evidence, and sealed artifacts.
- Its task envelope stays compatible with OpenBench's files-plus-checker
  contract, but the native sidecar states that Harbor execution is unsupported.
- Native and Harbor results are never merged into one denominator.

This is not a return to a generic pluggable execution abstraction. Each
host-only backend needs a concrete product reason and its own conformance,
privacy, and publication checks.

### Standard trial artifacts

Regardless of backend, each trial should produce a fixed machine-readable
package:

- resolved task, image, agent, model, and configuration identities;
- verifier reward, exit status, stdout, and stderr;
- agent trajectory and scrubbed logs;
- final workspace patch or snapshot evidence;
- timing, token usage, retries, and failure evidence;
- an immutable job/config lock sufficient to reproduce the trial.

### Remaining questions

- Which additional host-only benchmark families justify a native backend?
- What minimum live proof is required before native results become publishable?
- Can simple tasks share one minimal pinned image without making private task
  authoring cumbersome?
- How should trusted-machine identity be disclosed without publishing private
  paths, signing identities, or credentials?

### Proof required for a native backend

- Run matched, interleaved trials with the task, harness, and model fixed.
- Compare checker verdict, final state, model calls, usage, action telemetry,
  timings, retry behavior, and artifact completeness.
- Prove baseline validation fails and golden validation passes through the exact
  production verifier path.
- Prove OAuth works without credentials appearing in workspaces, logs,
  trajectories, or published artifacts.
- Prove the agent process cannot access hidden checker assets.
- Prove focus, lock state, MCP delivery, and monitor health cover the full agent
  phase on the real host.
