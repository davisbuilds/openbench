# CLI version pinning

OpenBench uses one CLI version set per dataset. The authoritative versions are the `ARG *_VERSION` pins in `bench/docker/Dockerfile`; both local and Docker execution must correspond to those pins.

At run start, `bench/run.py` reads those pins through `bench/bump_clis.py` and probes each host CLI with `--version` whenever a local lane is possible. This includes `--exec local` and Docker runs that allow local fallback. A mismatch stops the run before any cell executes:

```text
Refusing to start: host CLI versions do not match Dockerfile pins.
  grokbuild: host=0.2.91 pin=0.2.93 (grok --version)
Fix host CLIs: python3 bench/bump_clis.py --sync-host
```

Use `python3 bench/bump_clis.py --sync-host` to install each npm-based CLI at its exact pin. Cursor is not npm-distributed, so the command prints the pinned version and manual instructions instead. This mode does not build or modify a Docker image. `python3 bench/doctor.py` shows each host version beside its pin as `OK` or `DRIFT`.

`--allow-version-drift` is an explicit emergency waiver. If used, every newly emitted result row records `version_drift=true`; normal rows record `false`. Do not combine waived and unwaived rows as one experimental dataset without treating that field as a configuration difference.

A pure Docker invocation with `--no-docker-fallback` does not probe host CLIs because no host CLI can execute. After changing a Dockerfile pin with `bench/bump_clis.py --apply`, rebuild and use the verified image before collecting rows.
