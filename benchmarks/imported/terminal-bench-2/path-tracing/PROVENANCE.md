# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `path-tracing`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/path-tracing@sha256:4b44725102cd627dfa895191a5a30d16dae3c5623bb43da6459f79135db6b482`
- **Image size (`docker inspect .Size`)**: `399390468` bytes
- **Workspace source**: exact initial `/app` contents extracted from the pinned published task image, reproducing Dockerfile COPY/RUN effects as flat files.
- **Encrypted oracle assets**: none used; no `protected.tar.gz.enc` is referenced by this task.
- **Upstream category**: `software-engineering`

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the extracted flat-file starting state in `workspace/`.
- Copied upstream `solution/solve.sh` and materialized its deliverables in `solution/`.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, which mounts the graded workspace at `/app`, mounts checker-owned tests read-only, emits `SCORE: <v>`, and exits 0 iff the reward is 1.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-path-tracing:pinned`
- **Derived local image digest**: `openbench-tb2-path-tracing@sha256:b344efc45769e7ce3074f115d9aad208227efc8c9d64628890a55bfd1ed303a4`
- **Derived image size (`docker inspect .Size`)**: `482774105` bytes
- **Rebuild recipe**: `docker build --platform linux/amd64 -t openbench-tb2-path-tracing:pinned checker_data/image`
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- Removed runtime `apt-get`, `curl`, and uv installation from `checker_data/tests/test.sh`.
- Replaced upstream `uvx` execution with direct `pytest`; pytest 8.4.1 and pytest-json-ctrf 0.3.5 are installed in the derived image at build time.
- Exposed the build-time uv-managed Python 3.13 as `/usr/local/bin/python3` and `/usr/local/bin/python`.
- Preinstalled upstream verifier dependencies `pillow==11.2.1` and `numpy==2.3.1` in the pytest tool environment.
