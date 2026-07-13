FROM alexgshaw/dna-assembly@sha256:d1adf6835f1dd91205ba70e452c699d0aea601010038e5617f370716efb50569
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates primer3 && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN uv tool install --python 3.13 --with pytest-json-ctrf==0.3.5 pytest==8.4.1 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python3 \
 && ln -sf "$(uv python find 3.13)" /usr/local/bin/python \
 && pytest --version && python3 --version
