FROM alexgshaw/path-tracing@sha256:4b44725102cd627dfa895191a5a30d16dae3c5623bb43da6459f79135db6b482
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN uv tool install --python 3.13 --with pytest-json-ctrf==0.3.5 --with pillow==11.2.1 --with numpy==2.3.1 pytest==8.4.1 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python3 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python \
 && pytest --version && python3 --version
