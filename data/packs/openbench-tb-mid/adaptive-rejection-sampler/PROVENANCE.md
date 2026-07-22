# adaptive-rejection-sampler provenance

- Upstream: Terminal-Bench 2 `adaptive-rejection-sampler` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (Apache-2.0).
- Image: `openbench-tb2-adaptive-rejection-sampler:pinned`
- Dockerfile SHA-256: `82344bdebd3f5b7f0c8af24594eb90281e8f05f0e09485ec7ac80acc9ba3ee77`
- Image digest: `openbench-tb2-adaptive-rejection-sampler@sha256:b5df629425a80e7ced5d68628bd2ee4d16523a6ec9c09f42b5fea900c0e1a575`
- Checker: upstream pytest verifier, checker-owned under `checker_data/`, executed with `--network none`.
- Oracle review: upstream solve is checker-owned and only activated by the solution overlay marker; acceptance data is not agent-visible.
- License/provenance note: Upstream assets and notices are preserved unchanged.

- Image extension: composes the pinned `openbench-harness:latest` CLI runtime after the upstream task and offline-verifier layers; task dependencies and native workdir remain upstream-defined.
