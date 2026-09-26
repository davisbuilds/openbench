# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `dna-assembly`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/dna-assembly@sha256:d1adf6835f1dd91205ba70e452c699d0aea601010038e5617f370716efb50569`
- **Image size (`docker inspect .Size`)**: `29727501` bytes
- **Workspace source**: exact initial `/app` contents extracted from the pinned published task image, reproducing Dockerfile COPY/RUN effects as flat files.
- **Encrypted oracle assets**: none used; no `protected.tar.gz.enc` is referenced by this task.
- **Upstream category**: `scientific-computing`

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the extracted flat-file starting state in `workspace/`.
- Copied upstream `solution/solve.sh` and materialized its deliverables in `solution/`.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, which mounts the graded workspace at `/app`, mounts checker-owned tests read-only, emits `SCORE: <v>`, and exits 0 iff the reward is 1.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-dna-assembly:pinned`
- **Derived local image digest**: `openbench-tb2-dna-assembly@sha256:18b25281f10b5080fcbe55f8b82d4b03f65e0a46efb653efe067c1f46bb01363`
- **Derived image size (`docker inspect .Size`)**: `95480990` bytes
- **Rebuild recipe**: `docker build --platform linux/amd64 -t openbench-tb2-dna-assembly:pinned checker_data/image`
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- Removed runtime `apt-get`, `curl`, and uv installation from `checker_data/tests/test.sh`.
- Replaced upstream `uvx` execution with direct `pytest`; pytest 8.4.1 and pytest-json-ctrf 0.3.5 are installed in the derived image at build time.
- Exposed the build-time uv-managed Python 3.13 as `/usr/local/bin/python3` and `/usr/local/bin/python`.
- Installed the verifier-only `primer3` Debian package at image build time because upstream tests invoke `oligotm`.
