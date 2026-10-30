ARG TARGETARCH

# Build context is assembled by scripts/local/build_repair_runtime.py.
# It never contains a task, oracle, home directory, or repository history.
FROM golang:1.26.10-bookworm@sha256:632dff7351799a5a9954a48dc06670ade72ee44e8c351cdc0e5a57af2fea21af AS go
# PBS archives carry pip 26.2.2 and its updated vendored CA bundle.
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS python-amd64
ADD --checksum=sha256:6d7521138fd33afddc1ccd08abb007d9a2829e38117c2576f9efdc0ec73ce772 https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.12.15%2B20261021-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz /tmp/python.tar.gz
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS python-arm64
ADD --checksum=sha256:b4567aa5a11f51b540c4dfd9e082ebf23e554f8ac17b85caa6cea23e83693394 https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.12.15%2B20261021-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz /tmp/python.tar.gz
FROM python-${TARGETARCH} AS python
RUN mkdir /opt/python && tar -xzf /tmp/python.tar.gz --strip-components=1 -C /opt/python
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS node
COPY --from=python /opt/python /usr/local
WORKDIR /opt/repair-deps
COPY node/package.json node/package-lock.json ./
RUN apt-get update --error-on=any -o Acquire::AllowInsecureRepositories=false -o Acquire::AllowDowngradeToInsecureRepositories=false -o Debug::Acquire::gpgv=true && apt-get install -y --no-install-recommends make g++ \
    && npm ci --ignore-scripts --no-audit --no-fund \
    && rm -rf node_modules/better-sqlite3/prebuilds \
    && node_modules/.bin/node-gyp rebuild --release --force_build=1 \
         --directory=node_modules/better-sqlite3 --nodedir=/usr/local \
    && npm prune --omit=dev --ignore-scripts --no-audit --no-fund
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205
COPY --from=python /opt/python /usr/local
RUN apt-get update --error-on=any -o Acquire::AllowInsecureRepositories=false -o Acquire::AllowDowngradeToInsecureRepositories=false -o Debug::Acquire::gpgv=true && apt-get install -y --no-install-recommends \
      git ripgrep bash jq build-essential curl ca-certificates procps
COPY --from=go /usr/local/go /usr/local/go
RUN ln -s /usr/local/go/bin/go /usr/local/bin/go \
    && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt
RUN mkdir -p /opt/openbench/apt-evidence \
    && cp -a /etc/apt/sources.list.d /opt/openbench/apt-evidence/ \
    && cp /var/lib/apt/lists/*InRelease /opt/openbench/apt-evidence/ \
    && apt-cache policy > /opt/openbench/apt-evidence/policy.txt \
    && dpkg-query -W > /opt/openbench/apt-evidence/packages.tsv \
    && rm -rf /var/lib/apt/lists/*
COPY obench/runtime_trust.py /tmp/runtime_trust.py
COPY trust/blocked-root.pem /tmp/blocked-root.pem
RUN python3 /tmp/runtime_trust.py --known-positive /tmp/blocked-root.pem --output /opt/openbench/runtime-trust.json \
    && rm /tmp/runtime_trust.py /tmp/blocked-root.pem
COPY python/requirements.txt /opt/repair-python/requirements.txt
COPY tools/ /opt/repair-tools/
RUN npm ci --prefix /opt/repair-tools --ignore-scripts --no-audit --no-fund \
    && ln -s /opt/repair-tools/node_modules/.bin/codex /usr/local/bin/codex \
    && ln -s /opt/repair-tools/node_modules/.bin/pnpm /usr/local/bin/pnpm \
    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes --no-deps -r /opt/repair-python/requirements.txt \
    && groupadd --gid 10001 solver \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash solver \
    && mkdir -p /app /logs/agent /logs/verifier /logs/artifacts /installed-agent /run/openbench-model \
    && codex --version
COPY --from=node /opt/repair-deps /opt/repair-deps
RUN ln -s /opt/repair-deps/node_modules /node_modules \
    && ln -s /opt/repair-deps/node_modules/typescript/bin/tsc /usr/local/bin/tsc \
    && ln -s /opt/repair-deps/node_modules/tsx/dist/cli.mjs /usr/local/bin/tsx \
    && ln -s /opt/repair-deps/node_modules/eslint/bin/eslint.js /usr/local/bin/eslint \
    && ln -s /opt/repair-deps/node_modules/@tailwindcss/cli/dist/index.mjs /usr/local/bin/tailwindcss \
    && ln -s /opt/repair-deps/node_modules/vite/bin/vite.js /usr/local/bin/vite \
    && ln -s /opt/repair-deps/node_modules/svelte-check/bin/svelte-check /usr/local/bin/svelte-check \
    && node -e "const DB=require('/node_modules/better-sqlite3'); const db=new DB(':memory:'); db.prepare('SELECT 1').get(); db.close()"
COPY obench/ /opt/openbench/obench/
ENV PYTHONPATH=/opt/openbench PYTHONDONTWRITEBYTECODE=1 HOME=/home/solver \
    PATH=/usr/local/go/bin:/opt/repair-deps/node_modules/.bin:$PATH \
    GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOCACHE=/tmp/go-build GOPATH=/tmp/go \
    npm_config_cache=/tmp/npm-cache npm_config_offline=true \
    npm_config_manage_package_manager_versions=false \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_NO_INDEX=1 PNPM_HOME=/tmp/pnpm XDG_CACHE_HOME=/tmp/cache
LABEL org.openbench.developer-environment="4"
WORKDIR /app
