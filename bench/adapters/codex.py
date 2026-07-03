"""Adapter for the `codex` CLI (OpenAI Codex, ChatGPT-subscription login).

Headless invocation:
    codex exec --json --skip-git-repo-check -C <workdir> \
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
- `--json` emits a JSONL event stream. The final `turn.completed` event carries
  `usage={input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}`.
  Token accounting (see ``_parse_json``) uses the SAME fresh-basis as the other
  adapters:
    tokens = (input_tokens - cached_input_tokens) + output_tokens
             + reasoning_output_tokens   (cache re-reads excluded)
    turns  = number of `turn.completed` events (model rounds).
  Human tail is synthesized from `agent_message`/`file_change` items.
  NOTE: `codex exec --json` only flushes/exits cleanly when driven as a plain
  captured subprocess (stdin=DEVNULL); piping it through a shell can stall it.
  Parsing is defensive: shape drift yields tokens=None/turns=None + raw tail.
"""

import json
import os
import shutil
import subprocess

NAME = "codex"
_EXE = "codex"

# canonical model name -> codex `-m` model string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5",
}

# canonical model name -> reasoning effort passed via `-c model_reasoning_effort`
_EFFORT = {
    "gpt-5.5-medium": "medium",
}

# --- M4 open models (first-party pay-per-token, OpenAI-compatible) ----------
# Wired via codex's `-c model_providers.<id>.*` CLI overrides ONLY (never the
# user's ~/.codex/config.toml). wire_api="chat" because these vendors speak the
# Chat Completions API, not codex's default Responses API (base_url gets
# "/chat/completions" appended). Base URLs verified from official docs 2026-07.
# Key-gated: run() returns a SETUP-NEEDED dict if the env key is unset.
# (Duplicated across the pi/opencode/codex adapters so each stays self-contained
#  under the runner's isolated importer.)
OPEN_MODELS = {
    "glm-5.2":           {"provider": "zai",      "model_id": "glm-5.2",           "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM"},
    "glm-4.7-flash":     {"provider": "zai",      "model_id": "glm-4.7-flash",     "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM"},
    "deepseek-v4-flash": {"provider": "deepseek", "model_id": "deepseek-v4-flash", "base_url": "https://api.deepseek.com",     "env_key": "DEEPSEEK_API_KEY", "display": "DeepSeek"},
    "kimi-k2.7-code":    {"provider": "moonshot", "model_id": "kimi-k2.7-code",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi"},
}


def _unsupported(model):
    known = list(MODELS) + list(OPEN_MODELS)
    return {"completed": False, "error": f"unsupported-model: {model!r} (have {known})",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def _setup_needed(env_key, model):
    return {"completed": False,
            "error": f"SETUP-NEEDED: export {env_key} to use {model}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def version():
    """Return the CLI version string (with binary path), or None on failure.

    Cheap `codex --version`; never raises (the runner calls this defensively).
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
    """Parse codex's JSONL event stream into (tokens, turns, tail).

    tokens/turns are None when nothing parseable is found. tail is a
    human-readable transcript synthesized from agent_message / file_change items.
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

    tokens = 0
    found = False
    turns = 0
    transcript = []
    for ev in events:
        etype = ev.get("type")
        if etype == "turn.completed":
            turns += 1
            usage = ev.get("usage") or {}
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
                cached = usage.get("cached_input_tokens") or 0
                reasoning = usage.get("reasoning_output_tokens") or 0
                tokens += (int(inp) - int(cached)) + int(out) + int(reasoning)
                found = True
        elif etype == "item.completed":
            item = ev.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                text = item.get("text")
                if text:
                    transcript.append(text)
            elif itype == "file_change":
                names = [os.path.basename(c.get("path", ""))
                         for c in (item.get("changes") or [])
                         if isinstance(c, dict) and c.get("path")]
                transcript.append(f"[file_change: {', '.join(n for n in names if n)}]")
            elif itype == "command_execution":
                transcript.append("[command]")

    tokens = tokens if found else None
    turns = turns or None
    tail = "\n".join(transcript)[-2000:]
    return tokens, turns, tail


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    base = [
        "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "-C", workdir,
        "-s", "workspace-write",
    ]
    if model in MODELS:
        cmd = base + [
            "-m", MODELS[model],
            "-c", f'model_reasoning_effort="{_EFFORT[model]}"',
            instruction,
        ]
    elif model in OPEN_MODELS:
        spec = OPEN_MODELS[model]
        if not os.environ.get(spec["env_key"]):
            return _setup_needed(spec["env_key"], model)
        prov = spec["provider"]
        # NOTE: codex 0.142.x REMOVED wire_api="chat" — custom providers must use
        # the Responses API ("responses", the only accepted value). CONFIRMED via
        # live smoke (2026-07-03): codex authenticates + lists /models fine, but
        # the completion FAILS because Z.ai / DeepSeek / Moonshot only serve
        # /chat/completions, not /responses. pi and opencode (chat-completions)
        # solve the same models. So codex open-model support is effectively
        # BLOCKED for these chat-only providers; the wiring is kept for the day
        # codex restores chat-wire (or a provider adds /responses). See
        # discussions/7782. Use pi/opencode for the M4 open panel.
        cmd = base + [
            "-c", f'model_providers.{prov}.name="{spec["display"]}"',
            "-c", f'model_providers.{prov}.base_url="{spec["base_url"]}"',
            "-c", f'model_providers.{prov}.env_key="{spec["env_key"]}"',
            "-c", f'model_providers.{prov}.wire_api="responses"',
            "-c", f'model_provider="{prov}"',
            "-m", spec["model_id"],
            instruction,
        ]
    else:
        return _unsupported(model)

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
