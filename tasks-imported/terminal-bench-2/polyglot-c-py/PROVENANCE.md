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
