# query-optimize provenance

- Upstream: Terminal-Bench 2 `query-optimize` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (Apache-2.0).
- Image: `openbench-tb2-query-optimize:pinned`
- Dockerfile SHA-256: `aafdc214251ae6d989ed8d63e104b39bff96b759b5e2df8918f3407376c5935c`
- Image digest: `openbench-tb2-query-optimize@sha256:9e847e4af966d76e41363651967f2523e469bd8a8e517b5364a9f79fe95d4134`
- Checker: upstream pytest verifier, checker-owned under `checker_data/`, executed with `--network none`.
- Oracle review: upstream solve is checker-owned and only activated by the solution overlay marker; acceptance data is not agent-visible.
- License/provenance note: OEWN/WordNet database remains in the pinned upstream image; upstream dataset/license notice preserved.

- Verifier portability: uses 4 balanced timing repetitions and omits the expensive per-cell equivalence rerun between two immutable checker fixtures to fit OpenBench’s 120-second polarity gate; exact-output and the original 1.05x performance threshold remain enforced. OEWN is Open English WordNet (https://en-word.net/), distributed under CC BY 4.0; the pinned database URL and hash check remain unchanged.

- Image extension: composes the pinned `openbench-harness:latest` CLI runtime after the upstream task and offline-verifier layers; task dependencies and native workdir remain upstream-defined.
