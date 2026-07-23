# sanitize-git-repo provenance

- Upstream: Terminal-Bench 2 `sanitize-git-repo` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (Apache-2.0).
- Image: `openbench-tb2-sanitize-git-repo:pinned`
- Dockerfile SHA-256: `4379f706a46746dca12271a685f39b208de7bf88c55710ba54e3fbbfe5b7618b`
- Image digest: `openbench-tb2-sanitize-git-repo@sha256:d972cfdfdee6b175d3593a2b0bac469b9d8e06c3be64148650af427ec1995298`
- Checker: upstream pytest verifier, checker-owned under `checker_data/`, executed with `--network none`.
- Oracle review: upstream solve is checker-owned and only activated by the solution overlay marker; acceptance data is not agent-visible.
- License/provenance note: Embedded AWS/GitHub/HuggingFace-like credentials are synthetic test fixtures, confirmed by upstream FAKE_* verifier constants.

- Image extension: composes the pinned `openbench-harness:latest` CLI runtime after the upstream task and offline-verifier layers; task dependencies and native workdir remain upstream-defined.
