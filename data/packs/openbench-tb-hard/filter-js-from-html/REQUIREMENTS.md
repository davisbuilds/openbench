# Requirements (Harbor import)

This task was imported from a Harbor environment that may need more
than a bare workspace checkout.

- **Harbor base image**: `python:3.13-slim-bookworm`
- **OpenBench Docker lane**: map packages from that image into `openbench-harness:latest` (or a custom image) when using `--exec docker`.
- **Packages hinted from Dockerfile RUN**:
  - `chromium`
  - `chromium-driver`
  - `(pip packages — see Dockerfile RUN)`
- **Why DOCKER-REQUIRED**:
  - RUN directive: RUN apt-get update && apt-get install -y chromium chromium-driver && apt-get cle
  - RUN directive: RUN pip install --no-cache-dir selenium==4.35.0 beautifulsoup4==4.13.4
  - no local COPY/ADD sources staged into workspace (empty agent workspace — agent creates files from scratch)

The importer does **not** run Docker. Validate with
`obench validate --tasks-dir …` after extending the image if needed.
