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
