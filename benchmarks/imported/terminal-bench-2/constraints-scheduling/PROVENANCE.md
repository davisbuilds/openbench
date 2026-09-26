# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `constraints-scheduling`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/constraints-scheduling@sha256:567ce5a189f8d11ac461790876e934cc7af38391baf89a78f95f4dafc1fec3b0`
- **Image size (`docker inspect .Size`)**: `49852952` bytes
- **Workspace source**: `environment/inputs/` copied into `workspace/`, matching the Dockerfile `COPY inputs/ /app/`.
- **Encrypted oracle assets**: none found for this task in the shallow clone; no `protected.tar.gz.enc` was present.

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the OpenBench starting input calendars in `workspace/` from `environment/inputs/`.
- Copied upstream `solution/solve.sh` into `solution/` and materialized `meeting_scheduled.ics` there for OpenBench validation.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`; copied immutable input calendars to `checker_data/inputs/` for checker-owned integrity comparison.
- Added `checker.sh`, a host-run shim that launches the pinned Docker image, mounts the graded workspace at `/app`, mounts `checker_data/tests/` at `/tests`, mounts `checker_data/inputs/` at `/inputs`, reads `/logs/verifier/reward.txt`, emits `SCORE: <v>`, and exits 0 iff `v >= 1`.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-constraints-scheduling:pinned`
- **Derived local image digest**: `openbench-tb2-constraints-scheduling@sha256:4fd56d4a744e7f469c89a398486fe82a2bdceb7438b69b649d862f46dc231f9a`
- **Derived image size (`docker inspect .Size`)**: `112715691` bytes
- **Rebuild recipe**: `checker_data/image/Dockerfile` (`docker build --platform linux/amd64 -t openbench-tb2-constraints-scheduling:pinned checker_data/image`)
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- **Python shim**: the derived image exposes the build-time uv-managed Python as `/usr/local/bin/python3` and `/usr/local/bin/python` for tests that spawn Python subprocesses.
- **Exact runtime-download removals from `checker_data/tests/test.sh`**:
  - removed `apt-get update`
  - removed `apt-get install -y curl`
  - removed `curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh`
  - replaced the `uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest ...` invocation with direct `pytest ...`; pytest and the CTRF plugin are installed in the derived image at build time.
