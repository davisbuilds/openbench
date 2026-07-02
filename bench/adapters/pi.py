"""Adapter for the `pi` CLI (ChatGPT-subscription "openai-codex" route).

Headless invocation:
    HOME=<isolated tmp with only .pi/agent/auth.json>
    pi -p --no-extensions --provider openai-codex --model gpt-5.5 \
       --thinking medium <instruction>

Notes / quirks:
- pi loads the user's personal extensions from the real ~/.pi. One of them
  (pi-goal) crashes `-p` non-interactive mode. To avoid this WITHOUT touching
  the user's config, we run pi under an ISOLATED HOME: a fresh temp dir that
  contains ONLY `.pi/agent/auth.json` copied from the real one. No settings.json
  means no extensions are registered. `--no-extensions` is added as a belt-and-
  suspenders guard against any project-local extension discovery in workdir.
- Subscription route: provider `openai-codex` exposes `gpt-5.5`
  (verified via `pi --list-models`). The API-key `openai` provider also has
  gpt-5.5 but we prefer the subscription credential.
- Reasoning effort via `--thinking medium`.
- The real ~/.pi/agent/auth.json is only READ (copied), never modified.
"""

import os
import re
import shutil
import subprocess
import tempfile

NAME = "pi"

# canonical model name -> pi `--model` string (used with --provider openai-codex)
MODELS = {
    "gpt-5.5-medium": "gpt-5.5",
}

# canonical model name -> `--thinking` level
_THINKING = {
    "gpt-5.5-medium": "medium",
}

_PROVIDER = "openai-codex"
_REAL_AUTH = os.path.expanduser("~/.pi/agent/auth.json")

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
    if not os.path.exists(_REAL_AUTH):
        return {
            "completed": False,
            "error": f"missing pi auth at {_REAL_AUTH}",
            "output_tail": "",
            "tokens": None,
            "turns": None,
            "cmd": None,
        }

    cmd = [
        "pi", "-p",
        "--no-extensions",
        "--provider", _PROVIDER,
        "--model", MODELS[model],
        "--thinking", _THINKING[model],
        instruction,
    ]

    iso_home = tempfile.mkdtemp(prefix="pi_home_")
    try:
        agent_dir = os.path.join(iso_home, ".pi", "agent")
        os.makedirs(agent_dir, exist_ok=True)
        shutil.copy2(_REAL_AUTH, os.path.join(agent_dir, "auth.json"))

        env = dict(os.environ)
        env["HOME"] = iso_home

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
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
