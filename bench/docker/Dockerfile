# Isolation image for `python bench/run.py --exec docker`.
#
# One disposable container per benchmark cell runs the SAME adapter the host
# would (bench/entry.py imports bench/adapters/<harness>.py unchanged). This
# image therefore only needs: a Python 3 interpreter (to run entry.py + the
# stdlib-only adapters) and the harness CLIs on PATH. Auth is bind-mounted
# read-only at RUN TIME (see docker_exec.py) and is never baked in here.
#
# Build:
#   docker build -t openbench-harness:latest bench/docker
#   # include the three non-Node CLIs (best-effort Linux installs):
#   docker build --build-arg INSTALL_UNVERIFIED=true -t openbench-harness:latest bench/docker
#
# node base gives us node+npm for the two Node-based CLIs; python3 comes from
# apt. Both are required.
FROM node:22-slim

# python3 runs entry.py and the adapters; git/curl/ca-certificates support the
# CLIs and their installers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 ca-certificates curl git bash \
    && rm -rf /var/lib/apt/lists/*

# --- Verified Linux-portable harness CLIs (Node packages) ------------------
# Package names confirmed from the host install:
#   codex -> @openai/codex, pi -> @earendil-works/pi-coding-agent
RUN npm install -g \
        @openai/codex \
        @earendil-works/pi-coding-agent \
    && codex --version \
    && pi --version

# --- Best-effort native CLIs (opencode / cursor-agent / devin) -------------
# These ship as platform-specific binaries; their Linux installs are NOT yet
# verified in this image. Opt in with --build-arg INSTALL_UNVERIFIED=true.
# Kept out of the default build so the core image (codex + pi) always builds.
#
# NOTE: the exact opencode npm package name and the cursor/devin Linux
# installers must be confirmed before relying on this layer. Update the
# commands below once verified.
ARG INSTALL_UNVERIFIED=false
RUN if [ "$INSTALL_UNVERIFIED" = "true" ]; then \
        set -eux; \
        npm install -g opencode-ai; \
        curl -fsSL https://cursor.com/install | bash; \
        # devin: add the official Linux installer here once known, e.g.
        #   curl -fsSL https://<devin-install-url> | bash
        echo "opencode/cursor installed (best-effort); devin TODO"; \
    fi
ENV PATH="/root/.local/bin:${PATH}"

# entry.py + adapters are bind-mounted at run time (read-only) so the image
# does not need rebuilding when an adapter changes. WORKDIR is the mounted
# /work; the runner overrides it with -w /work.
WORKDIR /work

# Default command is a no-op; the runner always supplies:
#   python3 /bench/entry.py <harness> <model> <timeout_s>
CMD ["python3", "-c", "print('openbench-harness image; run via bench/run.py --exec docker')"]
