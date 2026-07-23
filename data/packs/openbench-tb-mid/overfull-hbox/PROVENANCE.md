# overfull-hbox provenance

- Upstream: Terminal-Bench 2 `overfull-hbox` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (Apache-2.0).
- Image: `openbench-tb2-overfull-hbox:pinned`
- Dockerfile SHA-256: `d4a249058f02abfae575e9e75d7170265bdd638fe4f024831b8addc741c7ef58`
- Image digest: `openbench-tb2-overfull-hbox@sha256:b30720af09e1818e4c906be3414edad593d3d271992c5257532d4f06f2ec1e05`
- Checker: upstream pytest verifier, checker-owned under `checker_data/`, executed with `--network none`.
- Oracle review: upstream solve is checker-owned and only activated by the solution overlay marker; acceptance data is not agent-visible.
- License/provenance note: Upstream assets and notices are preserved unchanged.

- Verifier portability: removed its runtime texlive reinstall; the exact pinned texlive package is already installed by the upstream Dockerfile.

- Image extension: composes the pinned `openbench-harness:latest` CLI runtime after the upstream task and offline-verifier layers; task dependencies and native workdir remain upstream-defined.
