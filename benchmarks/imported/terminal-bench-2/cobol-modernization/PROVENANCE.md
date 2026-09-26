# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `cobol-modernization`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/cobol-modernization@sha256:593ab9df3d83f771e927888b2d9436b380cdf7ec4d0af2a9f0802d541edebba0`
- **Image size (`docker inspect .Size`)**: `162020401` bytes
- **Workspace source**: `environment/src` and `environment/data` copied, matching Dockerfile COPYs into `/app`
- **Encrypted oracle assets**: none found for this task in the shallow clone; no `protected.tar.gz.enc` was present.

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the OpenBench starting state in `workspace/` from the source above.
- Copied upstream `solution/solve.sh` into `solution/` and materialized its deliverables there where needed for OpenBench validation.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, a host-run shim that launches the pinned Docker image, mounts the graded workspace at the original TB-2 workdir (`/app`), mounts `checker_data/tests/` at `/tests`, reads `/logs/verifier/reward.txt`, emits `SCORE: <v>`, and exits 0 iff `v >= 1`.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-cobol-modernization:pinned`
- **Derived local image digest**: `openbench-tb2-cobol-modernization@sha256:6a23e876b503be9a467d60f603c6874a151ada227c0e6b4184ac74226d875733`
- **Derived image size (`docker inspect .Size`)**: `188802710` bytes
- **Rebuild recipe**: `checker_data/image/Dockerfile` (`docker build --platform linux/amd64 -t openbench-tb2-cobol-modernization:pinned checker_data/image`)
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- **Python shim**: the derived image exposes the build-time uv-managed Python as `/usr/local/bin/python3` and `/usr/local/bin/python` for tests that spawn Python subprocesses.
- **Exact runtime-download removals from `checker_data/tests/test.sh`**:
  - removed `apt-get update`
  - removed `apt-get install -y curl`
  - removed `curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh`
  - replaced the `uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest ...` invocation with direct `pytest ...`; pytest and the CTRF plugin are installed in the derived image at build time.
