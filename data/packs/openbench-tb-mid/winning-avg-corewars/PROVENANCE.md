# winning-avg-corewars provenance

- Upstream: Terminal-Bench 2 `winning-avg-corewars` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (Apache-2.0).
- Image: `openbench-tb2-winning-avg-corewars:pinned`
- Dockerfile SHA-256: `8e76f686e1db2a8eb598c06523fbae405327ddb4442a24b788e51ac2be54db4b`
- Image digest: `openbench-tb2-winning-avg-corewars@sha256:6144649d4a2d9aa0bb25c86715b66e720274e53a37480cae0f1c26ae6edfb251`
- Checker: upstream pytest verifier, checker-owned under `checker_data/`, executed with `--network none`.
- Oracle review: upstream solve is checker-owned and only activated by the solution overlay marker; acceptance data is not agent-visible.
- License/provenance note: Upstream CoreWars warrior fixtures and their attribution remain unchanged.

- Image extension: composes the pinned `openbench-harness:latest` CLI runtime after the upstream task and offline-verifier layers; task dependencies and native workdir remain upstream-defined.
