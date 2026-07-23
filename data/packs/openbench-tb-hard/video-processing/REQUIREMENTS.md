# Requirements (Harbor import)

This task was imported from a Harbor environment that may need more
than a bare workspace checkout.

- **Harbor base image**: `python:3.13-slim-bookworm`
- **OpenBench Docker lane**: map packages from that image into `openbench-harness:latest` (or a custom image) when using `--exec docker`.
- **Packages hinted from Dockerfile RUN**:
  - `libgl1-mesa-glx`
  - `libglib2.0-0`
  - `libsm6`
  - `libxext6`
  - `libxrender-dev`
  - `libgomp1`
  - `(pip packages — see Dockerfile RUN)`
- **Why DOCKER-REQUIRED**:
  - RUN directive: RUN apt-get update && apt-get install -y libgl1-mesa-glx libglib2.0-0 libsm6 lib
  - RUN directive: RUN pip install "opencv-contrib-python>=4.11.0.86"

The importer does **not** run Docker. Validate with
`obench validate --tasks-dir …` after extending the image if needed.
