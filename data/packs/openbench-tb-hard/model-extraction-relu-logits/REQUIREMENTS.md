# Requirements (Harbor import)

This task was imported from a Harbor environment that may need more
than a bare workspace checkout.

- **Harbor base image**: `python:3.13-slim-bookworm`
- **OpenBench Docker lane**: map packages from that image into `openbench-harness:latest` (or a custom image) when using `--exec docker`.
- **Packages hinted from Dockerfile RUN**:
  - `(pip packages — see Dockerfile RUN)`
- **Why DOCKER-REQUIRED**:
  - RUN directive: RUN pip install numpy==2.2.5

The importer does **not** run Docker. Validate with
`obench validate --tasks-dir …` after extending the image if needed.
