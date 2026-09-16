# Repair benchmark isolation

Status: design and offline feasibility work, 2026-09-16. **Not implemented in
the live model route.** This is a prerequisite for the
[extra-hard repair tier](EXTRA_HARD_REPAIRS.md).

## What exists today

The native calibration had HOME isolation, not host read or network isolation.
Two attempts read a reference solution and hidden checker from another local
worktree. Those results remain withdrawn as difficulty evidence. SSH to another
machine would be another possible retrieval path; hiding local paths does not
remove that capability.

`scripts/local/verify_dojo_container.py` launches source-only Docker containers
with `--network none`, no mounts, dropped capabilities and no-new-privileges.
Its host-file canaries and task controls passed. That is an offline probe, not
the authenticated Harbor execution path. The direct Harbor reference trial
established task compatibility, not live transport confinement.

The existing counting proxy is a metering component, not a retrieval firewall:
its Codex route accepts multiple HTTP methods and forwards caller-selected path
tails and request bodies. Allowlisting its host would not limit other ports on
that host. Docker's ordinary `--internal` bridge also permits communication with
its gateway; it does not alone prove host exclusion. See
[Docker network internal mode](https://docs.docker.com/reference/cli/docker/network/create/#network-internal-mode---internal).

## Selected direction: no external networking in the solver

Keep the actual harness CLI and its tools inside the same disposable solver
container. Avoid replacing its tool loop with a different harness.

```text
solver container (network=none)
  Codex CLI -> loopback HTTP relay
                    |
           one per-trial Unix socket
                    |
trusted model gateway -> fixed provider inference endpoint

solver stopped -> validated source artifact -> fresh candidate worker
                                                  ^
                                      trusted external grader
```

The Unix socket lives on a dedicated Docker named volume, mounted read-only in
the solver. It is the only shared transport object: no host directory, Docker
socket, SSH agent socket, tailnet device, other trial volume or daily HOME.
Only the trusted gateway can create or replace the socket. The solver runs as
an unprivileged user, with all capabilities dropped, no-new-privileges, no host
PID/network namespace, bounded resources and writable task/scratch directories.
Bake dependencies and the pinned CLI into the image before untrusted execution.

The relay exposes HTTP only on solver loopback and forwards to that one socket.
It has no credentials and cannot select an upstream destination. Replacing the
relay or bypassing it still grants only the same gateway capability. There is
no external network interface through which knowing a Mini address, SSH command,
alternate port or DNS name should help.

This is a transport design to integrate with Harbor's environment/profile
extension points; Harbor continues to own trials, timeouts and cleanup. A
standalone Docker demonstration does not admit the canonical `obench run` route.

### Offline feasibility evidence

On 2026-09-16, a real Docker probe and independent replay passed on the laptop:
UID 10001, zero effective/bounding capabilities, no-new-privileges, read-only
root filesystem, `network=none`, and one read-only named-volume mount. A fake
gateway returned a host-generated nonce over the Unix socket; its independently
captured request log matched the client's challenge. Loopback TCP worked;
external IPv4 TCP attempts to ports 443, 53 and 22 returned `ENETUNREACH`.
Writing the socket directory and creating a raw socket failed. Created containers
and volumes were removed.

Local script and receipts: `results/sandbox-topology/`, with the independent
replay under `root-replay/`. Both sides were offline: this proves Unix-socket
plumbing, not an HTTP relay, gateway policy, provider connectivity, Codex auth,
Harbor integration, or a real Mini SSH denial. TCP port 53 is not a full DNS
test; UDP, IPv6 and a known-reachable external control remain admission work.

## The gateway is part of the security boundary

Use a fresh gateway identity and socket per trial. Keep provider credentials
outside the solver and bind its short-lived capability to one model, effort,
request budget and lifetime. Revoke it when the trial ends. Keep credential
staging and any refresh in the already-authorized execution lane.

Implement an explicit request contract, not a generic HTTP/CONNECT proxy:

- Fixed upstream, method and inference path; no arbitrary URLs, route selection,
  redirects, account/history endpoints, downloads or generic tunnels.
- Validate request bodies against the pinned CLI's actual protocol. Reject
  provider-hosted web search, remote MCP, file retrieval and other server tools;
  retain only the harness's required client-executed tool declarations. Also
  reject request features that cause provider-side URL fetching.
- Disable harness search, external MCP/apps/plugins and any alternate model
  transport. Enforce restrictions at the gateway as well as in harness config;
  the agent can edit its own config.
- Bound request sizes, concurrency and usage; count every accepted request.
  Do not turn a protocol mismatch into a public-network fallback.

Real Codex subscription transport, auth refresh, streaming and treatment identity
remain integration questions. Prove them using the exact pinned CLI before
claiming a drop-in equivalent. A fake response tests socket plumbing only.

The pinned Harbor source supports an environment `import_path` and separate
verifier mode. A narrow environment extension can enforce fixed no-network
policy and reject conflicting phase policies, mounts and compose overrides.
Do not assume stock Docker launch matches the offline probe: the current task
defaults to root, and Harbor's ordinary mount list includes writable verifier
logs. Remove that authority from the solver. Artifact collection must also stop
and freeze the solver first; a mode named "separate verifier" is not sufficient
evidence of safe ordering or safe file transfer.

## Grading is another authority boundary

Returning from the harness command does not prove all agent processes stopped.
Never put hidden tests in a solver container that may still contain a watcher.
Stop its processes before extracting a frozen artifact, revoke model access,
validate permitted paths/types/sizes without following symlinks or special files,
and transfer only permitted source changes into a fresh execution environment.
Do not preserve candidate-modified system files, CLI binaries or config.

A fresh grader container alone is insufficient if it imports arbitrary candidate
code with access to the checker and reward files. Keep the oracle and scoring
process outside the candidate worker. Give the worker test inputs and observe
its outputs/state; the trusted grader computes rewards. The current Dojo oracle
imports candidate Python modules in the verifier process and needs adaptation.
Harbor's separate-verifier mode is a useful lifecycle seam, not by itself proof
of this stronger boundary.

## Admission before another calibration

Run paired controls through the actual canonical entry point, with receipts
binding the image, CLI, model settings, broker policy and effective mounts:

| Capability | Must succeed | Must fail |
|---|---|---|
| Task work | Read/write source; run child processes | Host canary via direct path, symlink or subprocess |
| Model transport | Pinned CLI completes a bounded inference | Arbitrary route, CONNECT, alternate model, server search or URL fetch |
| External network | Reach a controlled canary from an unrestricted control | Same known-reachable service from solver; host gateway, SSH-like TCP, IPv4/IPv6 and DNS paths |
| Ambient authority | Fresh intended config loads | SSH keys/agent, Docker socket, tailnet, other trials and daily config |
| Lifecycle | Frozen source reaches fresh worker | Background watcher survives to read a newly supplied secret |
| Grading | Buggy/reference/alternative repairs get expected scores | Candidate reads oracle or writes authoritative reward |

Observe endpoints, transferred artifacts and rewards from outside the subject.
An SSH-port refusal without a known-reachable control is not useful proof.
Inspect connected peers for the real gateway transport and verify its provider
route separately; an offline socket fixture cannot establish public egress
policy or successful authentication.

The design addresses answer retrieval by ordinary arbitrary tool commands, not
a proof against every kernel/container-runtime exploit. A dedicated disposable
worker VM with no personal repositories or tailnet membership provides an
additional containment layer. Running on the Mini alone does not: it also has
valuable files and network access. Neither design removes answers already in
model weights; report that residual and use new behavioral variants.
