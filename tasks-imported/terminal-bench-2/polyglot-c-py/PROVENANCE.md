# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `polyglot-c-py`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/polyglot-c-py@sha256:0f1c3b7816d70cf5551573fd6aeef76893f2ae3000be2419997b6871b5d987ed`
- **Image size (`docker inspect .Size`)**: `148543403` bytes
- **Workspace source**: empty `/app` workdir from pinned image
- **Encrypted oracle assets**: none found for this task in the shallow clone; no `protected.tar.gz.enc` was present.

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the OpenBench starting state in `workspace/` from the source above.
- Copied upstream `solution/solve.sh` into `solution/` and materialized its deliverables there where needed for OpenBench validation.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, a host-run shim that launches the pinned Docker image, mounts the graded workspace at the original TB-2 workdir (`/app`), mounts `checker_data/tests/` at `/tests`, reads `/logs/verifier/reward.txt`, emits `SCORE: <v>`, and exits 0 iff `v >= 1`.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-polyglot-c-py:pinned`
- **Derived local image digest**: `openbench-tb2-polyglot-c-py@sha256:01ea8df732bca2d0a3f924b31d61fd8b9d9f01932091626d3d11be2fb9f96060`
- **Derived image size (`docker inspect .Size`)**: `213745930` bytes
- **Rebuild recipe**: `checker_data/image/Dockerfile` (`docker build --platform linux/amd64 -t openbench-tb2-polyglot-c-py:pinned checker_data/image`)
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- **Python shim**: the derived image exposes the build-time uv-managed Python as `/usr/local/bin/python3` and `/usr/local/bin/python` for tests that spawn Python subprocesses.
- **Exact runtime-download removals from `checker_data/tests/test.sh`**:
  - removed `apt-get update`
  - removed `apt-get install -y curl`
  - removed `curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh`
  - replaced the `uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 pytest ...` invocation with direct `pytest ...`; pytest and the CTRF plugin are installed in the derived image at build time.
