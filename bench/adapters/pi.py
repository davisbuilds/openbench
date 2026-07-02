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
- `--mode json` emits a JSONL event stream. The final `agent_end` event carries
  `messages[]`, each assistant message holding
  `usage={input,output,cacheRead,cacheWrite,totalTokens}`; `turn_end` events
  mark model rounds. Token accounting (see ``_parse_json``):
    tokens = sum of input+output over assistant messages (fresh tokens; cache
             re-reads excluded, matching the other adapters' definition).
    turns  = number of `turn_end` events (model rounds).
  Parsing is defensive: shape drift yields tokens=None/turns=None + raw tail.
"""

import json
import os
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


def _parse_json(stdout):
    """Parse pi's JSONL event stream into (tokens, turns, tail).

    tokens/turns are None when nothing parseable is found. tail is the
    concatenated assistant text.
    """
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        return None, None, ""

    turns = sum(1 for e in events if e.get("type") == "turn_end") or None

    # Prefer the final agent_end message list; fall back to message_end events.
    messages = None
    for e in events:
        if e.get("type") == "agent_end" and isinstance(e.get("messages"), list):
            messages = e["messages"]
    if messages is None:
        messages = [e.get("message") for e in events
                    if e.get("type") == "message_end"]

    total = 0
    found = False
    transcript = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        # Usage is billed per assistant turn; restrict to assistant messages so
        # tool/user entries can never double-count.
        usage = msg.get("usage") or {}
        for key in ("input", "output"):
            val = usage.get(key)
            if isinstance(val, (int, float)):
                total += int(val)
                found = True
        for part in (msg.get("content") or []):
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                transcript.append(part["text"])

    tokens = total if found else None
    tail = "\n".join(transcript)[-2000:]
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
        "--mode", "json",
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
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
