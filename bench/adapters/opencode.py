"""Adapter for the `opencode` CLI (ChatGPT-subscription OAuth route).

Headless invocation:
    OPENAI_API_KEY unset in child env
    opencode run --dir <workdir> -m openai/gpt-5.5 --variant medium \
        --auto --format json <instruction>

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
- `--format json` emits a JSONL event stream. Each ``step_finish`` event is one
  model round and carries ``part.tokens={input,output,reasoning,cache{...}}``.
  Token accounting (see ``_parse_json``):
    tokens = sum of input+output+reasoning across step_finish events (fresh
             tokens actually processed; cache re-reads are excluded so a
             multi-step run isn't inflated by re-sent context).
    turns  = number of step_finish events (model rounds; one assistant message
             each).
  Parsing is defensive: on any shape drift it yields tokens=None/turns=None and
  the raw output as the tail, never raising.
"""

import json
import os
import shutil
import subprocess

NAME = "opencode"
_EXE = "opencode"

# canonical model name -> opencode `-m` model string (provider/model)
MODELS = {
    "gpt-5.5-medium": "openai/gpt-5.5",
}

# canonical model name -> `--variant` reasoning effort
_VARIANT = {
    "gpt-5.5-medium": "medium",
}

# --- M4 open models (first-party pay-per-token, OpenAI-compatible) ----------
# Wired via a custom provider passed through OPENCODE_CONFIG_CONTENT (inline
# JSON env var) so nothing touches the user's opencode config and the temp
# workspace stays clean. apiKey uses opencode's {env:VAR} interpolation. Base
# URLs verified from official docs 2026-07. Key-gated in run().
# (Duplicated across pi/opencode/codex so each adapter stays self-contained.)
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


def _open_config_content(spec):
    """Inline OPENCODE_CONFIG_CONTENT JSON registering the open provider."""
    prov = spec["provider"]
    return json.dumps({
        "provider": {
            prov: {
                "npm": "@ai-sdk/openai-compatible",
                "name": spec["display"],
                "options": {
                    "baseURL": spec["base_url"],
                    "apiKey": "{env:" + spec["env_key"] + "}",
                },
                "models": {spec["model_id"]: {}},
            }
        }
    })


def version():
    """Return the CLI version string (with binary path), or None on failure.

    Cheap `opencode --version`; never raises (the runner calls this defensively).
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
    return (_dec(exc.stdout) + _dec(exc.stderr))[-limit:]


def _parse_json(stdout):
    """Parse opencode's JSONL event stream into (tokens, turns, tail).

    tokens/turns are None if nothing parseable is found. tail is a
    human-readable transcript synthesized from text and tool events.
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
    turns = 0
    transcript = []
    for ev in events:
        etype = ev.get("type")
        part = ev.get("part") or {}
        if etype == "step_finish":
            turns += 1
            tok = part.get("tokens") or {}
            for key in ("input", "output", "reasoning"):
                val = tok.get(key)
                if isinstance(val, (int, float)):
                    tokens += int(val)
        elif etype == "text":
            text = part.get("text")
            if text:
                transcript.append(text)
        elif etype == "tool_use":
            tool = part.get("tool")
            if tool:
                transcript.append(f"[tool: {tool}]")

    tail = "\n".join(transcript)[-2000:]
    return (tokens or None), (turns or None), tail


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    env = dict(os.environ)
    if model in MODELS:
        cmd = [
            "opencode", "run",
            "--dir", workdir,
            "-m", MODELS[model],
            "--variant", _VARIANT[model],
            "--auto",
            "--format", "json",
            instruction,
        ]
        env.pop("OPENAI_API_KEY", None)  # force subscription OAuth route
    elif model in OPEN_MODELS:
        spec = OPEN_MODELS[model]
        if not os.environ.get(spec["env_key"]):
            return _setup_needed(spec["env_key"], model)
        cmd = [
            "opencode", "run",
            "--dir", workdir,
            "-m", f'{spec["provider"]}/{spec["model_id"]}',
            "--auto",
            "--format", "json",
            instruction,
        ]
        env["OPENCODE_CONFIG_CONTENT"] = _open_config_content(spec)
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
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        tail = _err_tail(e)
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
        # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr for the
        # runner's local transcript. LOCAL-ONLY; never published unscrubbed.
        "full_output": combined,
        "tokens": tokens,
        "turns": turns,
        "cmd": cmd,
    }
