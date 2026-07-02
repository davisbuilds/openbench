"""Adapter for the `opencode` CLI (ChatGPT-subscription OAuth route).

Headless invocation:
    OPENAI_API_KEY unset in child env
    opencode run --dir <workdir> -m openai/gpt-5.5 --variant medium \
        --auto <instruction>

Notes / quirks:
- OPENAI_API_KEY MUST be stripped from the child env. If it is present,
  opencode uses the API-key provider instead of the stored subscription
  OAuth credential (~/.local/share/opencode/auth.json); stripping it forces
  the subscription route (verified).
- `--variant medium` selects the reasoning effort for the model.
- `--auto` auto-approves tool permissions so file edits happen unattended.
  opencode `run` is non-interactive, but write/edit permission is otherwise
  gated; --auto is required for the agent to modify files headlessly.
- `--dir` sets the working directory the agent operates in.
"""

import os
import re
import subprocess

NAME = "opencode"

# canonical model name -> opencode `-m` model string (provider/model)
MODELS = {
    "gpt-5.5-medium": "openai/gpt-5.5",
}

# canonical model name -> `--variant` reasoning effort
_VARIANT = {
    "gpt-5.5-medium": "medium",
}

# opencode default output prints a "N tokens" / token summary in some builds.
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
        "opencode", "run",
        "--dir", workdir,
        "-m", MODELS[model],
        "--variant", _VARIANT[model],
        "--auto",
        instruction,
    ]

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)  # force subscription OAuth route

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
