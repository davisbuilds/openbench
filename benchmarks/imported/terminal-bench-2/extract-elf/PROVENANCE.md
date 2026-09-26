# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `extract-elf`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/extract-elf@sha256:6932e4cb318464307eacd497ef8dc617eaf551b6a90231f815ec0b911895cfed`
- **Image size (`docker inspect .Size`)**: `347389037` bytes
- **Workspace source**: exact initial `/app` contents extracted from the pinned published task image, reproducing Dockerfile COPY/RUN effects as flat files.
- **Encrypted oracle assets**: none used; no `protected.tar.gz.enc` is referenced by this task.
- **Upstream category**: `file-operations`

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the extracted flat-file starting state in `workspace/`.
- Copied upstream `solution/solve.sh` and materialized its deliverables in `solution/`.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, which mounts the graded workspace at `/app`, mounts checker-owned tests read-only, emits `SCORE: <v>`, and exits 0 iff the reward is 1.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-extract-elf:pinned`
- **Derived local image digest**: `openbench-tb2-extract-elf@sha256:6d2fa7f587b82fcbabcaf52c5a2af0fc5ed9609518a662ece585d6cc7ea0f84d`
- **Derived image size (`docker inspect .Size`)**: `409163995` bytes
- **Rebuild recipe**: `docker build --platform linux/amd64 -t openbench-tb2-extract-elf:pinned checker_data/image`
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- Removed runtime `apt-get`, `curl`, and uv installation from `checker_data/tests/test.sh`.
- Replaced upstream `uvx` execution with direct `pytest`; pytest 8.4.1 and pytest-json-ctrf 0.3.5 are installed in the derived image at build time.
- Exposed the build-time uv-managed Python 3.13 as `/usr/local/bin/python3` and `/usr/local/bin/python`.
- Hardened upstream requirement #1 enforcement so submitted addresses absent from the reference are rejected instead of silently accepted.
