"""Adapter for the `codex` CLI (OpenAI Codex, ChatGPT-subscription login).

Headless invocation:
    codex exec --skip-git-repo-check -C <workdir> \
        -s workspace-write \
        -m gpt-5.5 -c model_reasoning_effort="medium" <instruction>

Notes / quirks:
- `codex exec` is fully non-interactive; there are no approval prompts to
  suppress. `-s workspace-write` is the least-privileged sandbox that still
  lets the agent edit files inside the workspace root.
- The runner hands us a disposable temp dir that is usually NOT a git repo,
  so `--skip-git-repo-check` is required or codex refuses to start.
- Reasoning effort is set via a config override, not the model string. The
  canonical "-medium" suffix is mapped to model_reasoning_effort.
- Uses the user's existing `~/.codex` login as-is (read-only).
- codex exec prints a "tokens used: N" summary line we parse for usage.
"""

import os
import re
import subprocess

NAME = "codex"

# canonical model name -> codex `-m` model string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5",
}

# canonical model name -> reasoning effort passed via `-c model_reasoning_effort`
_EFFORT = {
    "gpt-5.5-medium": "medium",
}

_TOKENS_RE = re.compile(r"tokens used[:\s]+([\d,]+)", re.IGNORECASE)


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
        "codex", "exec",
        "--skip-git-repo-check",
        "-C", workdir,
        "-s", "workspace-write",
        "-m", MODELS[model],
        "-c", f'model_reasoning_effort="{_EFFORT[model]}"',
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
