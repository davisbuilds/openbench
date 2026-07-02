"""Adapter for the `cursor-agent` CLI (Cursor, existing login).

Headless invocation:
    cursor-agent -p --force --trust \
        --model gpt-5.5-medium --output-format text \
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
"""

import os
import re
import subprocess

NAME = "cursor"

# canonical model name -> cursor-agent `--model` string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5-medium",
}

_TOKENS_RE = re.compile(r"([\d,]+)\s+tokens", re.IGNORECASE)


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
        "--output-format", "text",
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
    m = _TOKENS_RE.search(combined)
    tokens = int(m.group(1).replace(",", "")) if m else None

    return {
        "completed": proc.returncode == 0,
        "error": None if proc.returncode == 0 else f"exit {proc.returncode}",
        "output_tail": combined[-2000:],
        "tokens": tokens,
        "turns": None,
        "cmd": cmd,
    }
