#!/usr/bin/env bash
# One-time Cursor Agent container login for the docker benchmark lane.
#
# Subscription route (preferred): this starts an interactive container with
# Linux cursor-agent using HOME=/cursor-home, bind-mounted to the persistent host
# directory below. Complete the browser login once; cursor-agent stores auth in
# FILES on Linux, empirically observed at:
#   /cursor-home/.config/cursor/auth.json      (credential, read by status/run)
#   /cursor-home/.cursor/cli-config.json       (CLI config written during login)
#
# Per benchmark run, bench/docker_exec.py mounts the corresponding host paths
# read-only back into the disposable container's /root HOME. The host directory
# is never baked into an image.
#
# API-key fallback: instead of subscription login, export CURSOR_API_KEY. The
# docker runner passes CURSOR_API_KEY through by name only when set.
#
# Usage:
#   bench/cursor_container_login.sh
#   # after completing login in the browser, verify:
#   docker run --rm -e HOME=/cursor-home \
#     -v "$HOME/.openbench/cursor-container-auth:/cursor-home" \
#     openbench-harness:latest sh -lc 'export PATH=/root/.local/bin:$PATH; cursor-agent status'
set -euo pipefail

IMAGE="${OPENBENCH_DOCKER_IMAGE:-openbench-harness:latest}"
AUTH_DIR="${CURSOR_CONTAINER_AUTH_DIR:-$HOME/.openbench/cursor-container-auth}"
NAME="cursorprobe_login_$$"

mkdir -p "$AUTH_DIR"
chmod 700 "$AUTH_DIR"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat >&2 <<EOF
Starting Cursor Agent login container.
Persistent host auth dir: $AUTH_DIR

Complete the browser step printed by cursor-agent. Do not paste tokens into
chat/logs. When the CLI reports success, exit the shell (Ctrl-D or exit).
EOF

docker run --rm -it \
  --name "$NAME" \
  -e HOME=/cursor-home \
  -e NO_OPEN_BROWSER=1 \
  -v "$AUTH_DIR:/cursor-home" \
  "$IMAGE" \
  sh -lc 'set -e; export PATH=/root/.local/bin:$PATH; command -v cursor-agent >/dev/null; cursor-agent login; echo; echo "Status:"; cursor-agent status; echo; echo "Auth files:"; find "$HOME/.config/cursor" "$HOME/.cursor" -maxdepth 2 -type f -print 2>/dev/null || true; exec sh'
