# Local OpenBench verifier image for TB-2 task prove-plus-comm.
# Build with:
#   docker build --platform linux/amd64 -t openbench-tb2-prove-plus-comm:pinned .
# Base is the pinned upstream Terminal-Bench-2 task image.
FROM alexgshaw/prove-plus-comm@sha256:e26741d01681a9da1beeff8b7b7b65fe2b921b8b122a306a3707a81ebd051f73

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime network is disabled by checker.sh. Install verifier-only tools at
# image build time instead of inside tests/test.sh.
RUN apt-get update  && apt-get install -y --no-install-recommends curl ca-certificates  && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Preinstall pytest and the CTRF plugin used by upstream tests. uv may download
# the requested Python at build time; checker runtime must not need network.
RUN uv tool install --python 3.13 --with pytest-json-ctrf==0.3.5 pytest==8.4.1 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python3 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python \
 && pytest --version \
 && python3 --version
