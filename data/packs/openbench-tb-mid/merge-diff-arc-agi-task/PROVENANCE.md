# merge-diff-arc-agi-task provenance

- Upstream: Terminal-Bench 2 `merge-diff-arc-agi-task` at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6` (Apache-2.0).
- Image: `openbench-tb2-merge-diff-arc-agi-task:pinned`
- Dockerfile SHA-256: `618c93fab9559caae5ede08f457b064f2beb332eca9f919c438e8dc0ec6ccff6`
- Image digest: `openbench-tb2-merge-diff-arc-agi-task@sha256:11dc550b6657fd5dbd4840823cf7c3c4fe07f94e2e06f24b7005aff1d28191ff`
- Checker: upstream pytest verifier, checker-owned under `checker_data/`, executed with `--network none`.
- Oracle review: upstream solve is checker-owned and only activated by the solution overlay marker; acceptance data is not agent-visible.
- License/provenance note: Upstream ARC fixtures and git bundles remain checker-owned/unchanged.

- Image extension: composes the pinned `openbench-harness:latest` CLI runtime after the upstream task and offline-verifier layers; task dependencies and native workdir remain upstream-defined.
