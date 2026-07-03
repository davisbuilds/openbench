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
- Reasoning effort is baked into the model name: `gpt-5.5-medium` is a
  first-class model id (verified via `cursor-agent models`).
- Uses the user's existing Cursor login as-is (read-only).
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

# canonical model name -> cursor-agent `--model` string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5-medium",
}


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


def _parse_json(stdout):
    """Parse cursor's single-object JSON result into (tokens, turns, tail).

    tokens is None if usage is absent/malformed. turns is always None (not
    reported in this format). tail is the `result` text when present.
    """
    stdout = stdout.strip()
    if not stdout:
        return None, None, ""
    obj = json.loads(stdout)  # caller guards with try/except
    usage = obj.get("usage") or {}
    tokens = 0
    for key in ("inputTokens", "outputTokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)):
            tokens += int(val)
    tail = obj.get("result")
    tail = tail if isinstance(tail, str) else ""
    return (tokens or None), None, tail[-2000:]


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model not in MODELS:
        return {
            "completed": False,
            "error": f"unsupported-model: {model!r} (have {list(MODELS)})",
            "output_tail": "",
            "tokens": None,
            "turns": None,
            "cmd": None,
        }

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
        tail = ((e.stdout or "") + (e.stderr or ""))[-2000:]
        return {
            "completed": False,
            "error": f"timeout after {timeout_s}s",
            "output_tail": tail,
            "tokens": None,
            "turns": None,
            "cmd": cmd,
        }

    combined = (proc.stdout or "") + (proc.stderr or "")
    try:
        tokens, turns, tail = _parse_json(proc.stdout or "")
    except Exception:  # noqa: BLE001 - never let usage parsing break a run
        tokens, turns, tail = None, None, ""
    if not tail:
        tail = combined[-2000:]

    return {
        "completed": proc.returncode == 0,
        "error": None if proc.returncode == 0 else f"exit {proc.returncode}",
        "output_tail": tail,
        "tokens": tokens,
        "turns": turns,
        "cmd": cmd,
    }
