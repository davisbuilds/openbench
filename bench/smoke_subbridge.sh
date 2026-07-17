#!/usr/bin/env bash
# Manual, metered subscription-bridge smoke: one tiny Grok Build prompt.
# This script never uses an OpenAI API key. CLIProxyAPI owns subscription OAuth.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

bridge_url="${CLIPROXYAPI_BASE_URL:-http://127.0.0.1:8317/v1}"

if ! command -v cliproxyapi >/dev/null 2>&1; then
  cat >&2 <<'EOF'
SETUP-NEEDED: CLIProxyAPI is not installed.
  brew install cliproxyapi
  configure subscription OAuth in /opt/homebrew/etc/cliproxyapi.conf
  start the daemon on localhost:8317, then rerun this script
EOF
  exit 2
fi

if ! python3 - "$bridge_url" <<'PY'
import socket
import sys
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1])
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit(1)
port = parsed.port or (443 if parsed.scheme == "https" else 80)
try:
    with socket.create_connection((parsed.hostname, port), timeout=1):
        pass
except OSError:
    raise SystemExit(1)
PY
then
  cat >&2 <<'EOF'
SETUP-NEEDED: CLIProxyAPI is not reachable.
  brew install cliproxyapi
  verify /opt/homebrew/etc/cliproxyapi.conf contains your Codex/ChatGPT OAuth setup
  start CLIProxyAPI on localhost:8317, then rerun this script
EOF
  exit 2
fi

if ! command -v grok >/dev/null 2>&1; then
  echo 'SETUP-NEEDED: install Grok Build CLI (`npm install -g @xai-official/grok`).' >&2
  exit 2
fi

# Defense in depth: the adapter also removes this variable from the child.
unset OPENAI_API_KEY

python3 - "$bridge_url" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import tempfile

import sys
sys.path.insert(0, str(Path("bench").resolve()))
import proxy

adapter_path = Path("bench/adapters/grokbuild.py").resolve()
spec = importlib.util.spec_from_file_location("smoke_subbridge_grokbuild", adapter_path)
grokbuild = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grokbuild)

bridge_url = sys.argv[1]
from urllib.parse import urlsplit, urlunsplit
parsed = urlsplit(bridge_url)
bridge_origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

with tempfile.TemporaryDirectory(prefix="openbench_subbridge_smoke_") as tmp:
    ledger = Path(tmp) / "ledger"
    work = Path(tmp) / "work"
    work.mkdir()
    server, thread = proxy.start_in_thread(
        "127.0.0.1", 0, ledger, openai_upstream=bridge_origin, timeout_s=120)
    try:
        host, port = server.server_address[:2]
        os.environ["OPENBENCH_PROXY"] = "1"
        os.environ["OPENBENCH_PROXY_BASE_URL"] = f"http://{host}:{port}"
        os.environ["OPENBENCH_PROXY_CELL_TOKEN"] = "subbridge-smoke"
        os.environ["CLIPROXYAPI_BASE_URL"] = bridge_url
        result = grokbuild.run(
            "Reply with exactly OK. Do not use tools.", str(work), "gpt-5.6", 120)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    rows = []
    ledger_path = ledger / "subbridge-smoke.jsonl"
    if ledger_path.exists():
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line]
    if not result.get("completed"):
        raise SystemExit(f"SMOKE FAILED: {result.get('error') or 'unknown harness error'}")
    if len(rows) != 1:
        raise SystemExit(f"SMOKE FAILED: expected one metered model call, observed {len(rows)}")
    if rows[0].get("status") != 200 or rows[0].get("route") != "openai":
        raise SystemExit("SMOKE FAILED: metered CLIProxyAPI request was not successful")
    print("SMOKE OK: grokbuild -> counting proxy -> CLIProxyAPI -> Codex subscription (1 call)")
PY
