# Requirements (Harbor import)

This task was imported from a Harbor environment that may need more
than a bare workspace checkout.

- **Harbor base image**: `ubuntu:24.04`
- **OpenBench Docker lane**: map packages from that image into `openbench-harness:latest` (or a custom image) when using `--exec docker`.
- **Why DOCKER-REQUIRED**:
  - RUN directive: RUN apt update -y && apt install -y rustc g++ && rm -rf /var/lib/apt/lists/*
  - no local COPY/ADD sources staged into workspace (empty agent workspace — agent creates files from scratch)

The importer does **not** run Docker. Validate with
`obench validate --tasks-dir …` after extending the image if needed.
