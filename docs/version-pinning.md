# Reliability gates

OpenBench uses five complementary gates to prevent invalid or wasteful benchmark arms:

1. **Host version gate** compares locally executable harness CLIs with the Dockerfile pins. It catches an outdated or prematurely upgraded host CLI before any cell runs.
2. **Image-label gate** compares the selected harnesses' `org.openbench.cli.*` image labels with the same pins. It catches stale or incorrectly built benchmark images without launching a container.
3. **Upstream version check** is a weekly/manual GitHub Action that reports newer published CLI releases. It catches pins that need human review and an intentional dataset upgrade.
4. **Infra circuit breaker** is on by default in `obench run`. Three consecutive `infra` or `rate_limited` cells with fewer than 100 reported agent tokens abort the remaining invocation after preserving rows already written. `--max-consecutive-infra N` changes the streak threshold; `0` disables it. Wrong answers, timeouts, and infra failures with real token spend reset the streak, so capability failures do not stop an arm.
5. **Preflight smoke** is enabled with `--preflight-smoke`. Before main cells it runs one smoke task (prefers `make-it-run` when present in the resolved tasks dir; otherwise the first runnable task) as trial 0 with the invocation's harness/model/execution settings and writes only to a sibling `<results-stem>.preflight.jsonl` sidecar. Near-zero-token infra/rate-limit failure refuses the arm; a wrong answer is allowed because it proves the live route worked. `--allow-preflight-failure` is the explicit emergency override.

The first three gates preserve version integrity. The final two catch expired authentication, broken proxy routes, stale runtime images, and similar fast infrastructure failures before they can produce a garbage arm.

## CLI version pinning

OpenBench uses one CLI version set per dataset. The authoritative versions are the `ARG *_VERSION` pins in `obench/docker/Dockerfile`; both local and Docker execution must correspond to those pins.

At run start, `obench run` enforces both sides of the invariant before any cell executes:

- **Host gate:** it reads the pins through `obench/bump_clis.py` and probes each host CLI with `--version` whenever a local lane is possible. This includes `--exec local` and Docker runs that allow local fallback.
- **Image gate:** whenever Docker cells are requested, it performs one cheap `docker inspect` of build-time `org.openbench.cli.*` labels and compares the selected harness CLIs with the current Dockerfile pins. It does not start a container or probe every CLI.

A mismatch in either gate stops the run before any cell executes:

```text
Refusing to start: CLI versions do not match Dockerfile pins.
  grokbuild: host=0.2.91 pin=0.2.93 (grok --version)
Fix host CLIs: python3 -m obench.bump_clis --sync-host
```

A stale image refusal names each affected CLI and includes the rebuild command:

```text
Refusing to start: CLI versions do not match Dockerfile pins.
  pi: image=0.80.6 pin=0.80.10
Fix the image: docker build -t openbench-harness:latest obench/docker
Or update pins and build: python3 -m obench.bump_clis --apply
```

Use `python3 -m obench.bump_clis --sync-host` to install each npm-based CLI at its exact pin. Cursor is not npm-distributed, so the command prints the pinned version and manual instructions instead. This mode does not build or modify a Docker image. `obench doctor` shows each host version beside its pin as `OK` or `DRIFT`, and reports the image-label comparison alongside it.

`--allow-version-drift` is an explicit emergency waiver. If used, every newly emitted result row records `version_drift=true`; normal rows record `false`. Do not combine waived and unwaived rows as one experimental dataset without treating that field as a configuration difference.

Docker is fail-closed by default: a pure `--exec docker` invocation does not fall back to the host when the image is missing. It still runs the image gate and prints the build hint, then stops before any cell. Pass `--docker-fallback` to opt into whole-run local homogenization after a failed image inspect (host CLIs are probed because a local lane is then possible). Mid-run per-cell docker→local fallbacks abort so a results file never mixes `exec_mode` lanes. Availability is not recorded as version drift. After changing a Dockerfile pin with `python3 -m obench.bump_clis --apply`, rebuild and use the verified image before collecting rows.

## Upstream version automation

The weekly `CLI version check` GitHub Action (also available through manual dispatch) runs `python3 -m obench.bump_clis --check-upstream`. It queries npm’s `latest` tag for each npm-distributed CLI, reports Cursor as a manual check, and opens or updates the single **CLI version bumps available** issue when a pin is behind. Registry lookup failures are warnings and are skipped so one unavailable package does not hide the remaining report.

A human reviews that issue and upstream release notes, then runs `python3 -m obench.bump_clis --apply`, `python3 -m obench.bump_clis --sync-host`, and rebuilds the image. Together, the weekly upstream check, host gate, and image gate maintain the invariant: upstream changes are surfaced, local-capable runs cannot use drifted host CLIs, and Docker runs cannot silently use a stale prebuilt image.
