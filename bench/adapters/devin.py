"""Adapter for the `devin` CLI (Cognition Devin, terminal mode).

Headless invocation:
    devin -p --permission-mode accept-edits --model gpt-5.5 -- <instruction>

Notes / quirks:
- `-p/--print` runs non-interactively (process prompt, exit).
- `--permission-mode accept-edits` auto-approves read-only tools AND workspace
  edits, so file changes happen unattended. It does NOT auto-approve arbitrary
  destructive actions (that would be "dangerous"), so it's the least-privileged
  mode that lets the agent edit files.
- In print mode `--respect-workspace-trust` defaults to false, so a fresh temp
  workdir does not trigger a trust prompt. No extra flag needed.
- The prompt is passed after `--` so a leading dash in an instruction can never
  be parsed as a flag.
- cwd=workdir; the agent edits files there.
- MODEL / REASONING EFFORT CAVEAT: devin's `--model` takes a bare model id
  (e.g. "codex", "claude-opus-4.6", "gpt-5.5"). It exposes NO separate
  reasoning-effort knob, so the canonical "-medium" suffix maps to plain
  "gpt-5.5"; effort is whatever devin's default is for that model. This is the
  one harness where the "medium" pin is not independently verifiable.
- Uses the user's existing devin login as-is (read-only).
"""

import re
import subprocess

NAME = "devin"

# canonical model name -> devin `--model` string.
# devin has no reasoning-effort selector, so "-medium" collapses to "gpt-5.5".
MODELS = {
    "gpt-5.5-medium": "gpt-5.5",
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
        "devin", "-p",
        "--permission-mode", "accept-edits",
        "--model", MODELS[model],
        "--", instruction,
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
