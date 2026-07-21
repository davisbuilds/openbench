# Requirements (Harbor import)

This task was imported from a Harbor environment that may need more
than a bare workspace checkout.

- **Harbor base image**: `python:3.11`
- **OpenBench Docker lane**: map packages from that image into `openbench-harness:latest` (or a custom image) when using `--exec docker`.
- **Packages hinted from Dockerfile RUN**:
  - `git`
  - `tmux`
  - `asciinema`
- **Why DOCKER-REQUIRED**:
  - RUN directive: RUN apt-get update && apt-get install -y git  tmux asciinema && rm -rf /var/lib/

The importer does **not** run Docker. Validate with
`obench validate --tasks-dir …` after extending the image if needed.
