"""Adapter for the `cursor-agent` CLI (Cursor, existing login).

Headless invocation:
    cursor-agent -p --force --trust \
        --model gpt-5.5-medium --output-format json \
        --workspace <workdir> <instruction>

Notes / quirks:
- `-p/--print` runs non-interactively (has write + shell tools).
- `--force` auto-allows tool calls unless explicitly denied, so edits happen
  unattended.
- `--trust` trusts the workspace without prompting (only valid with --print).
  Required so the disposable temp workdir doesn't trigger a trust prompt.
- `--workspace <workdir>` (plus cwd=workdir) points the agent at the task dir;
  edits land there.
- Reasoning effort is baked into the model name: `gpt-5.5-medium` and
  `claude-opus-4-8-thinking-medium` are first-class model ids (verified via
  `cursor-agent models`).
- Uses Cursor auth from Linux file storage (`~/.config/cursor/auth.json`) or the
  documented `CURSOR_API_KEY` fallback. Docker auth should be minted with
  `bench/cursor_container_login.sh` and mounted read-only per run.
- COUNTING PROXY UNSUPPORTED for benchmark inference. Cursor's hidden
  `CURSOR_API_ENDPOINT` / `--agent-endpoint` overrides can route control-plane
  Connect-RPC calls through `bench/proxy.py`, and this adapter contains the
  unit-tested endpoint wiring for future CLI compatibility. However the shipped
  CLI's model stream uses Cursor's private HTTP/2 agent protocol and protobuf
  usage, while the stdlib counting proxy is HTTP/1.1 JSON/SSE. Leaving the
  server-selected agent URL in place bypasses the proxy; forcing the proxy URL
  fails with `RetriableError: [internal] Protocol error`. Thus the runner marks
  Cursor unsupported rather than breaking otherwise valid cells or claiming
  independently measured tokens. This is distinct from local macOS auth; a
  Docker-authenticated live probe reproduced the protocol boundary.
- M4 OPEN MODELS (glm-*/deepseek-*/kimi-*) are NOT supported here: cursor-agent
  exposes a closed, account-bound model menu with no custom-provider/base-URL
  override, so open canonicals fall through to the unsupported-model dict.
- `--output-format json` emits ONE final JSON object with a `result` text field
  and `usage={inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}`.
  Token accounting (see ``_parse_json``):
    tokens = inputTokens + outputTokens (fresh tokens; cache re-reads excluded).
    turns  = None -- the json result exposes no per-message/turn count; counting
             turns would require the heavier `--output-format stream-json` event
             stream. Left as None per ADAPTER_SPEC (report it only if available).
  Parsing is defensive: shape drift yields tokens=None and the raw tail.
"""

import json
import os
import shutil
import subprocess
import tempfile

NAME = "cursor"
_EXE = "cursor-agent"


def _empty_token_usage():
    return {
        "tokens_input_uncached": None,
        "tokens_cache_read": None,
        "tokens_cache_write": None,
        "tokens_output": None,
        "tokens_reasoning": None,
        "usage_raw": None,
        "token_basis": None,
    }


def _num(value):
    return int(value) if isinstance(value, (int, float)) else None

# canonical model name -> cursor-agent `--model` string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5-medium",
    "gpt-5.6-sol": "gpt-5.6-sol-medium",
    "gpt-5.6-terra": "gpt-5.6-terra-medium",
    "gpt-5.6-luna": "gpt-5.6-luna-medium",
    # Thinking parity for the opus frontier lane: Cursor exposes a concrete
    # medium-thinking model id, so no separate effort flag is needed.
    "claude-opus-4-8": "claude-opus-4-8-thinking-medium",
}

# Linux cursor-agent stores subscription auth in FILES, not a keychain. The
# docker lane stages the host-persistent login dir into this path; CURSOR_API_KEY
# remains the documented env fallback.
_CURSOR_AUTH_CANDIDATES = (
    os.path.expanduser("~/.config/cursor/auth.json"),
    os.path.expanduser("~/.openbench/cursor-container-auth/.config/cursor/auth.json"),
)
_CURSOR_AUTH = next((path for path in _CURSOR_AUTH_CANDIDATES if os.path.isfile(path)),
                    _CURSOR_AUTH_CANDIDATES[0])
_CURSOR_CLI_CONFIG = os.path.expanduser("~/.cursor/cli-config.json")


def _proxy_cell_url():
    base = os.environ.get("OPENBENCH_PROXY_BASE_URL")
    token = os.environ.get("OPENBENCH_PROXY_CELL_TOKEN")
    if not os.environ.get("OPENBENCH_PROXY") or not base or not token:
        return None
    return f"{base.rstrip('/')}/cell/{token}/cursor"


def version():
    """Return the CLI version string (with binary path), or None on failure.

    Cheap `cursor-agent --version`; never raises (runner calls it defensively).
    """
    try:
        proc = subprocess.run(
            [_EXE, "--version"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - version probing must never raise
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    path = shutil.which(_EXE)
    return f"{out} ({path})" if path else out


def _err_tail(exc, limit=2000):
    """Last `limit` chars of a TimeoutExpired's captured output, decoding safely.

    On TimeoutExpired, `.stdout`/`.stderr` may be bytes (even under text=True),
    str, or None. Concatenating bytes with the ``""`` fallback raises TypeError,
    so decode each part first — the handler must always yield a clean tail.
    """
    def _dec(x):
        if x is None:
            return ""
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
    text = _dec(exc.stdout) + _dec(exc.stderr)
    return text if limit is None else text[-limit:]


def _parse_json_with_usage(stdout):
    """Parse cursor's single-object JSON result into (tokens, turns, tail).

    tokens is None if usage is absent/malformed. turns is always None (not
    reported in this format). tail is the `result` text when present.
    """
    stdout = stdout.strip()
    if not stdout:
        return None, None, "", _empty_token_usage()
    obj = json.loads(stdout)  # caller guards with try/except
    usage = obj.get("usage") or {}
    tokens = 0
    found = False
    for key in ("inputTokens", "outputTokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)):
            tokens += int(val)
            found = True
    token_usage = _empty_token_usage()
    if found:
        inp = _num(usage.get("inputTokens"))
        out = _num(usage.get("outputTokens"))
        cache_read = _num(usage.get("cacheReadTokens"))
        cache_write = _num(usage.get("cacheWriteTokens"))
        if None not in (inp, out):
            token_usage.update({
                "tokens_input_uncached": inp,
                "tokens_cache_read": cache_read,
                "tokens_cache_write": cache_write,
                "tokens_output": out,
                "tokens_reasoning": None,
            })
        # Cursor's JSON surface is harness-reported rather than independently
        # vendor-verified, so keep the basis explicit even when split fields are
        # available for the runner's fresh-token smoke check.
        token_usage["usage_raw"] = usage
        token_usage["token_basis"] = "harness_reported"
    tail = obj.get("result")
    tail = tail if isinstance(tail, str) else ""
    return (tokens if found else None), None, tail[-2000:], token_usage


def _setup_needed(model):
    return {"completed": False,
            "error": (f"SETUP-NEEDED: run bench/cursor_container_login.sh for subscription auth "
                      f"(missing {_CURSOR_AUTH}) or export CURSOR_API_KEY to use {model}"),
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}



def _parse_json(stdout):
    """Backward-compatible parser returning legacy fields only."""
    tokens, turns, tail, token_usage = _parse_json_with_usage(stdout)
    return tokens, turns, tail

def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model not in MODELS:
        return {
            "completed": False,
            "error": f"unsupported-model: {model!r} (have {list(MODELS)})",
            "output_tail": "",
            "tokens": None,
            "turns": None,
            "cmd": None,
            **_empty_token_usage(),
        }
    if (model == "claude-opus-4-8" and os.environ.get("BENCH_IN_CONTAINER")
            and not (os.environ.get("CURSOR_API_KEY") or os.path.exists(_CURSOR_AUTH))):
        return _setup_needed(model)

    iso_home = tempfile.mkdtemp(prefix="cursor_home_")
    env = dict(os.environ)
    env["HOME"] = iso_home
    env["XDG_CONFIG_HOME"] = os.path.join(iso_home, ".config")
    env["XDG_DATA_HOME"] = os.path.join(iso_home, ".local", "share")
    env["XDG_STATE_HOME"] = os.path.join(iso_home, ".local", "state")
    env["XDG_CACHE_HOME"] = os.path.join(iso_home, ".cache")
    proxy_endpoint = _proxy_cell_url()
    if proxy_endpoint:
        env["CURSOR_API_ENDPOINT"] = proxy_endpoint
    # Preserve only file-based subscription auth. Do not copy ~/.cursor
    # cli-config.json, rules, MCPs, extensions, or any adjacent Cursor config.
    if os.path.isfile(_CURSOR_AUTH):
        auth_dest = os.path.join(env["XDG_CONFIG_HOME"], "cursor", "auth.json")
        os.makedirs(os.path.dirname(auth_dest), exist_ok=True)
        shutil.copy2(_CURSOR_AUTH, auth_dest)
    elif os.path.isfile(_CURSOR_CLI_CONFIG):
        # macOS cursor-agent stores auth and preferences in one JSON file.
        # Re-serialize only authInfo into the isolated config; copying the file
        # wholesale would import model, permissions, network, and UI choices.
        try:
            with open(_CURSOR_CLI_CONFIG, encoding="utf-8") as fh:
                auth_info = json.load(fh).get("authInfo")
        except (OSError, json.JSONDecodeError, AttributeError):
            auth_info = None
        if auth_info is not None:
            config_dest = os.path.join(iso_home, ".cursor", "cli-config.json")
            os.makedirs(os.path.dirname(config_dest), exist_ok=True)
            with open(config_dest, "w", encoding="utf-8") as fh:
                json.dump({"authInfo": auth_info}, fh)

    cmd = [
        "cursor-agent", "-p",
        *(["--endpoint", proxy_endpoint,
           "--agent-endpoint", proxy_endpoint,
           "--http-version", "1.1"] if proxy_endpoint else []),
        "--force",
        "--trust",
        "--model", MODELS[model],
        "--output-format", "json",
        "--workspace", workdir,
        instruction,
    ]

    try:
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            full_output = _err_tail(e, limit=None)
            return {
                "completed": False,
                "error": f"timeout after {timeout_s}s",
                "output_tail": full_output[-2000:],
                "full_output": full_output,
                "tokens": None,
                "turns": None,
                "cmd": cmd,
                **_empty_token_usage(),
            }
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)

    combined = (proc.stdout or "") + (proc.stderr or "")
    try:
        tokens, turns, tail, token_usage = _parse_json_with_usage(proc.stdout or "")
    except Exception:  # noqa: BLE001 - never let usage parsing break a run
        tokens, turns, tail, token_usage = None, None, "", _empty_token_usage()
    if not tail:
        tail = combined[-2000:]

    return {
        "completed": proc.returncode == 0,
        "error": None if proc.returncode == 0 else f"exit {proc.returncode}",
        "output_tail": tail,
        # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr for the
        # runner's local transcript. LOCAL-ONLY; never published unscrubbed.
        "full_output": combined,
        "tokens": tokens,
        "turns": turns,
        "cmd": cmd,
        **token_usage,
    }
