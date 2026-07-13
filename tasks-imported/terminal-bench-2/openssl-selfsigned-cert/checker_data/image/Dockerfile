FROM alexgshaw/openssl-selfsigned-cert@sha256:4c948a4e630af2435ae0a19108fc0814a946ac2fa29a512469e0fc77b38c8c12
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN uv tool install --python 3.13 --with pytest-json-ctrf==0.3.5 pytest==8.4.1 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python3 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python \
 && pytest --version && python3 --version
