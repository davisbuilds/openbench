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
#
# node base gives us node+npm for the Node-based CLIs; python3 comes from apt.
# Both are required.
FROM node:22-slim

ARG CODEX_VERSION=0.144.5
ARG PI_VERSION=0.80.10
ARG CLAUDE_VERSION=2.1.214
ARG GROK_VERSION=0.2.103
ARG OPENCODE_VERSION=1.18.3
ARG CURSOR_AGENT_VERSION=2026.07.09-a3815c0

# Cheap runtime integrity stamp: one `docker inspect` can compare the image's
# baked pins with this Dockerfile without starting a container or probing CLIs.
LABEL org.openbench.cli.codex="${CODEX_VERSION}" \
      org.openbench.cli.pi="${PI_VERSION}" \
      org.openbench.cli.claude="${CLAUDE_VERSION}" \
      org.openbench.cli.grok="${GROK_VERSION}" \
      org.openbench.cli.opencode="${OPENCODE_VERSION}" \
      org.openbench.cli.cursor="${CURSOR_AGENT_VERSION}"

ENV PATH="/root/.local/bin:${PATH}"
ENV CURSOR_AGENT_VERSION=${CURSOR_AGENT_VERSION}

# python3 runs entry.py and the adapters; git/curl/ca-certificates support the
# CLIs and their installers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 ca-certificates curl git bash \
    && rm -rf /var/lib/apt/lists/*

# --- Harness CLIs ----------------------------------------------------------
# Package names confirmed from the host install / scratch probes:
#   codex -> @openai/codex, pi -> @earendil-works/pi-coding-agent,
#   claude -> @anthropic-ai/claude-code (open models only; auth via env keys),
#   grok -> @xai-official/grok (Grok Build; open models via BYOK config),
#   opencode -> opencode-ai.
RUN npm install -g \
        @openai/codex@${CODEX_VERSION} \
        @earendil-works/pi-coding-agent@${PI_VERSION} \
        @anthropic-ai/claude-code@${CLAUDE_VERSION} \
        @xai-official/grok@${GROK_VERSION} \
        opencode-ai@${OPENCODE_VERSION} \
    && codex --version \
    && pi --version \
    && claude --version \
    && grok --version \
    && opencode --version

# cursor-agent is not npm-distributed; install the version-addressed Linux
# package directly and fail the build if the executable reports anything else.
RUN set -eux; \
    case "$(uname -m)" in \
        x86_64|amd64) cursor_arch="x64" ;; \
        arm64|aarch64) cursor_arch="arm64" ;; \
        *) echo "unsupported cursor-agent architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    cursor_dir="/root/.local/share/cursor-agent/versions/${CURSOR_AGENT_VERSION}"; \
    mkdir -p "$cursor_dir" /root/.local/bin; \
    curl -fsSL "https://downloads.cursor.com/lab/${CURSOR_AGENT_VERSION}/linux/${cursor_arch}/agent-cli-package.tar.gz" \
        | tar --strip-components=1 -xzf - -C "$cursor_dir"; \
    ln -sf "$cursor_dir/cursor-agent" /root/.local/bin/agent; \
    ln -sf "$cursor_dir/cursor-agent" /root/.local/bin/cursor-agent; \
    test "$(cursor-agent --version)" = "$CURSOR_AGENT_VERSION"

# Stamp the in-image CLI versions once at build time. Docker-mode benchmark rows
# read this file and record these container versions, never the host versions.
RUN python3 - <<'PY'
import json
import subprocess

commands = {
    "codex": ["codex", "--version"],
    "pi": ["pi", "--version"],
    "claude": ["claude", "--version"],
    "grokbuild": ["grok", "--version"],
    "opencode": ["opencode", "--version"],
    "cursor": ["cursor-agent", "--version"],
}
versions = {
    name: subprocess.check_output(cmd, text=True).strip()
    for name, cmd in commands.items()
}
with open("/etc/openbench-cli-versions.json", "w", encoding="utf-8") as fh:
    json.dump(versions, fh, sort_keys=True)
    fh.write("\n")
PY

# entry.py + adapters are bind-mounted at run time (read-only) so the image
# does not need rebuilding when an adapter changes. WORKDIR is the mounted
# /work; the runner overrides it with -w /work.
WORKDIR /work

# Default command is a no-op; the runner always supplies:
#   python3 /bench/entry.py <harness> <model> <timeout_s>
CMD ["python3", "-c", "print('openbench-harness image; run via bench/run.py --exec docker')"]
