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

# canonical model name -> cursor-agent `--model` string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5-medium",
    # Thinking parity for the opus frontier lane: Cursor exposes a concrete
    # medium-thinking model id, so no separate effort flag is needed.
    "claude-opus-4-8": "claude-opus-4-8-thinking-medium",
}

# Linux cursor-agent stores subscription auth in FILES, not a keychain. The
# docker lane stages the host-persistent login dir into this path; CURSOR_API_KEY
# remains the documented env fallback.
_CURSOR_AUTH = os.path.expanduser("~/.config/cursor/auth.json")


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
        # Cursor's JSON surface is harness-reported and not proven vendor-split;
        # preserve the legacy scalar and leave normalized split lanes unknown.
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

    cmd = [
        "cursor-agent", "-p",
        "--force",
        "--trust",
        "--model", MODELS[model],
        "--output-format", "json",
        "--workspace", workdir,
        instruction,
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
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
