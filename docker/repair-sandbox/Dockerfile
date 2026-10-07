# Build context is assembled by scripts/local/build_repair_runtime.py.
# It never contains a task, oracle, home directory, or repository history.
FROM node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS node
WORKDIR /opt/repair-deps
COPY node/package.json node/package-lock.json ./
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ \
    && npm ci --no-audit --no-fund \
    && rm -rf node_modules/better-sqlite3/prebuilds \
    && node_modules/.bin/node-gyp rebuild --release --force_build=1 \
         --directory=node_modules/better-sqlite3 --nodedir=/usr/local \
    && npm prune --omit=dev --ignore-scripts --no-audit --no-fund
FROM python:3.12-slim-bookworm@sha256:31c0807da611e2e377a2e9b566ad4eb038ac5a5838cbbbe6f2262259b5dc77a0
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep bash jq build-essential curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global @openai/codex@0.157.0 pnpm@10.29.3 \
    && python3 -m pip install --no-cache-dir PyYAML==6.0.3 pytest==8.3.5 \
         iniconfig==2.1.0 packaging==25.0 pluggy==1.6.0 \
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
    PATH=/opt/repair-deps/node_modules/.bin:$PATH \
    npm_config_cache=/tmp/npm-cache npm_config_offline=true \
    PIP_NO_INDEX=1 PNPM_HOME=/tmp/pnpm XDG_CACHE_HOME=/tmp/cache
LABEL org.openbench.developer-environment="3"
WORKDIR /app
