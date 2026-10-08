# Context contains only these pinned browser dependencies. No tasks or graders.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
WORKDIR /opt/browser-deps
COPY package.json package-lock.json ./
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browser-cache
RUN npm_config_offline=false npm ci --ignore-scripts --no-audit --no-fund \
    && node_modules/.bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /opt/browser-deps/node_modules/playwright /opt/repair-deps/node_modules/playwright \
    && ln -s /opt/browser-deps/node_modules/playwright-core /opt/repair-deps/node_modules/playwright-core \
    && ln -s /opt/browser-deps/node_modules/.bin/playwright /usr/local/bin/playwright \
    && ln -s /opt/browser-deps/node_modules/.bin/playwright-cli /usr/local/bin/playwright-cli
LABEL org.openbench.browser-environment="1"
COPY sandbox_gateway.py /opt/openbench/obench/sandbox_gateway.py
WORKDIR /app
