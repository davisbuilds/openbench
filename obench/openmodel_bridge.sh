#!/usr/bin/env bash
# Open-model Responses<->Chat bridge for the `codex` harness.
#
# WHY: codex-cli >=0.142 dropped wire_api="chat"; custom model providers must
# speak the Responses API. DeepSeek / Z.ai / Moonshot only serve
# /chat/completions, so codex cannot talk to them directly. This starts a
# host-side LiteLLM proxy that accepts /v1/responses ingress from codex and
# translates each call to /chat/completions upstream (per-provider LiteLLM
# routes: deepseek/ zai/ moonshot/ auto-bridge; an openai/ prefix would instead
# hit the provider's non-existent native /responses endpoint).
#
# LIFECYCLE: runs in the FOREGROUND; the benchmark runner does NOT manage it.
# A human/orchestrator starts this before open-model codex runs and Ctrl-C's it
# after. bench/adapters/codex.py probes the port and returns a SETUP-NEEDED
# error if the bridge is down.
#
# KEYS: read from the environment, falling back to ~/.openbench/keys.env. Never
# hardcoded, never printed.
#
# LAYOUT: config + hook are versioned in the repo (bench/bridge/); only the heavy
# litellm venv and the secret keys live outside the repo under ~/.openbench.
#
# BINDING: binds 0.0.0.0 by default so bench containers can reach it via
# host.docker.internal (Docker Desktop). The proxy has NO ingress auth and
# injects real provider keys upstream, so run it ONLY on a trusted single-user
# node (the benchmark Mac). Override the bind with BENCH_BRIDGE_BIND=127.0.0.1
# for host-only (non-docker) runs.
#
# Env overrides:
#   BENCH_BRIDGE_PORT   (default 4141)   -- must match the adapter default
#   BENCH_BRIDGE_BIND   (default 0.0.0.0)
#   OPENBENCH_HOME      (default ~/.openbench)  -- venv + keys live here
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$HERE/bridge"
OPENBENCH_HOME="${OPENBENCH_HOME:-$HOME/.openbench}"
PORT="${BENCH_BRIDGE_PORT:-4141}"
BIND="${BENCH_BRIDGE_BIND:-0.0.0.0}"
VENV="$OPENBENCH_HOME/bridge-venv"
CONFIG="$BRIDGE_DIR/config.yaml"
KEYS="$OPENBENCH_HOME/keys.env"

if [ ! -x "$VENV/bin/litellm" ]; then
  echo "ERROR: litellm not found at $VENV/bin/litellm" >&2
  echo "Install it (outside the repo):" >&2
  echo "  uv venv --python 3.12 $VENV" >&2
  echo "  uv pip install --exclude-newer '7 days' --exclude-newer-package 'certifi=2026-10-21T00:00:00Z' \\" >&2
  echo "    --python $VENV/bin/python 'litellm[proxy]!=1.107.0'" >&2
  exit 1
fi

# Inspect files from the shell before loading keys or starting this venv's
# Python: LiteLLM 1.107.0 executes its payload at interpreter startup.
refuse_installation() {
  echo "ERROR: unsafe or unverified LiteLLM bridge installation; refusing to start." >&2
  echo "Do not run this venv's Python or pip. Investigate startup hooks and rebuild the venv from vetted artifacts." >&2
  echo "If LiteLLM 1.107.0 was ever installed, treat the host as compromised and rotate exposed credentials." >&2
  exit 1
}
shopt -s nullglob
site_dirs=("$VENV"/lib/python*/site-packages "$VENV"/Lib/site-packages)
[ "${#site_dirs[@]}" -gt 0 ] || refuse_installation
metadata=()
for site in "${site_dirs[@]}"; do
  for indicator in "$site"/litellm-1.107.0.dist-info "$site"/cook.pth "$site"/cook.py \
      "$site"/sitecustomize.py "$site"/__pycache__/cook*.pyc "$site"/__pycache__/sitecustomize*.pyc; do
    [ ! -e "$indicator" ] || refuse_installation
  done
  metadata+=("$site"/litellm-*.dist-info/METADATA)
done
shopt -u nullglob
[ "${#metadata[@]}" -eq 1 ] || refuse_installation
installed_version="$(awk '/^Version: / {sub(/\r$/, "", $2); print $2}' "${metadata[0]}")"
[[ "$installed_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*$ ]] || refuse_installation
case "$installed_version" in
  1.107.0*) refuse_installation ;;
esac
if [ ! -f "$CONFIG" ]; then
  echo "ERROR: bridge config not found at $CONFIG" >&2
  exit 1
fi

# Load provider keys if not already exported. `set -a` exports everything the
# file defines; values are never echoed.
if [ -f "$KEYS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$KEYS"
  set +a
fi

missing=()
for v in ANTHROPIC_API_KEY DEEPSEEK_API_KEY ZAI_API_KEY MOONSHOT_API_KEY OPENROUTER_API_KEY; do
  if [ -z "${!v:-}" ]; then missing+=("$v"); fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "WARN: unset provider keys: ${missing[*]} (those models will 401)" >&2
fi

echo "Starting open-model bridge on ${BIND}:${PORT} (Ctrl-C to stop)" >&2
# Put bench/bridge/ on PYTHONPATH so the config's callback module (hooks.py) is
# importable regardless of the proxy's working directory.
export PYTHONPATH="$BRIDGE_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV/bin/litellm" --config "$CONFIG" --host "$BIND" --port "$PORT"
