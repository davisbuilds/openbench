#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CAP_ROOT="$ROOT/ablation/captures"
mkdir -p "$CAP_ROOT"
if [ -f "$HOME/.openbench/keys.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.openbench/keys.env"
  set +a
fi
: "${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY required in env or ~/.openbench/keys.env}"
BRIDGE_PORT="${BENCH_BRIDGE_PORT:-4141}"
if ! nc -z 127.0.0.1 "$BRIDGE_PORT" >/dev/null 2>&1; then
  echo "Starting bench/openmodel_bridge.sh on 127.0.0.1:${BRIDGE_PORT}" >&2
  (cd "$ROOT" && BENCH_BRIDGE_BIND=127.0.0.1 BENCH_BRIDGE_PORT="$BRIDGE_PORT" bench/openmodel_bridge.sh > "$CAP_ROOT/bridge.log" 2>&1 & echo $! > "$CAP_ROOT/bridge.pid")
  for _ in {1..60}; do nc -z 127.0.0.1 "$BRIDGE_PORT" >/dev/null 2>&1 && break; sleep 1; done
fi
if ! nc -z 127.0.0.1 "$BRIDGE_PORT" >/dev/null 2>&1; then
  echo "Bridge did not become reachable" >&2
  exit 1
fi
PROXY_PORT=4142
for variant in v0 v1 v2; do
  rm -rf "$CAP_ROOT/$variant" && mkdir -p "$CAP_ROOT/$variant"
  python3 "$ROOT/ablation/tools/capture_proxy.py" --listen-port "$PROXY_PORT" --target "http://127.0.0.1:${BRIDGE_PORT}" --capture-dir "$CAP_ROOT/$variant" > "$CAP_ROOT/$variant/proxy.log" 2>&1 &
  proxy_pid=$!
  trap 'kill $proxy_pid 2>/dev/null || true' RETURN
  for _ in {1..20}; do nc -z 127.0.0.1 "$PROXY_PORT" >/dev/null 2>&1 && break; sleep 0.2; done
  home="$ROOT/ablation/codex-home-$variant"
  echo "Running codex $variant" >&2
  CODEX_HOME="$home" DEEPSEEK_API_KEY="openbench-bridge-placeholder" \
    codex exec --json --skip-git-repo-check -C "$ROOT" -s read-only -m deepseek-v4-flash \
    "Ablation probe only. Reply with exactly: OK" \
    > "$CAP_ROOT/$variant/codex.stdout.jsonl" 2> "$CAP_ROOT/$variant/codex.stderr.txt" || true
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  trap - RETURN
done
rm -rf "$CAP_ROOT/pi" && mkdir -p "$CAP_ROOT/pi"
python3 "$ROOT/ablation/tools/capture_proxy.py" --listen-port "$PROXY_PORT" --target "http://127.0.0.1:${BRIDGE_PORT}" --capture-dir "$CAP_ROOT/pi" > "$CAP_ROOT/pi/proxy.log" 2>&1 &
proxy_pid=$!
trap 'kill $proxy_pid 2>/dev/null || true' RETURN
for _ in {1..20}; do nc -z 127.0.0.1 "$PROXY_PORT" >/dev/null 2>&1 && break; sleep 0.2; done
PI_HOME="$CAP_ROOT/pi-home"
rm -rf "$PI_HOME" && mkdir -p "$PI_HOME"
echo "Running pi" >&2
HOME="$PI_HOME" DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
  pi -p --no-extensions --no-context-files -e "$ROOT/ablation/tools/pi-deepseek-provider.mjs" \
  --provider deepseek-bridge --model deepseek-v4-flash --thinking medium --mode json \
  "Ablation probe only. Reply with exactly: OK" \
  > "$CAP_ROOT/pi/pi.stdout.jsonl" 2> "$CAP_ROOT/pi/pi.stderr.txt" || true
kill "$proxy_pid" 2>/dev/null || true
wait "$proxy_pid" 2>/dev/null || true
trap - RETURN
echo "Captures in $CAP_ROOT" >&2
