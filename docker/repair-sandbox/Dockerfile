# Build context is assembled by scripts/local/build_repair_runtime.py.
# It never contains a task, oracle, home directory, or repository history.
FROM node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS node
WORKDIR /opt/repair-deps
COPY node/package.json node/package-lock.json ./
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ \
    && npm ci --omit=dev --no-audit --no-fund
FROM python:3.12-slim-bookworm@sha256:31c0807da611e2e377a2e9b566ad4eb038ac5a5838cbbbe6f2262259b5dc77a0
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN node /usr/local/lib/node_modules/npm/bin/npm-cli.js install --global @openai/codex@0.154.0 \
    && python3 -m pip install --no-cache-dir PyYAML==6.0.3 \
    && groupadd --gid 10001 solver \
    && useradd --uid 10001 --gid 10001 --create-home solver \
    && mkdir -p /app /logs/agent /logs/verifier /logs/artifacts /installed-agent /run/openbench-model \
    && codex --version
COPY --from=node /opt/repair-deps /opt/repair-deps
RUN ln -s /opt/repair-deps/node_modules /node_modules \
    && node -e "const DB=require('/node_modules/better-sqlite3'); const db=new DB(':memory:'); db.prepare('SELECT 1').get(); db.close()"
COPY obench/ /opt/openbench/obench/
ENV PYTHONPATH=/opt/openbench PYTHONDONTWRITEBYTECODE=1 HOME=/home/solver
WORKDIR /app
