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
_EXE = "pi"


def version():
    """Return the CLI version string (with binary path), or None on failure.

    Cheap `pi --version` (short-circuits before extensions load, so no isolated
    HOME needed); never raises (the runner calls this defensively).
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


# --- M4 open models (first-party pay-per-token, OpenAI-compatible) ----------
# Wired via a pi provider EXTENSION written into the isolated temp HOME and
# loaded with `-e` (works even under --no-extensions). Nothing touches the
# user's ~/.pi. apiKey uses pi's "$ENV_KEY" env resolution. Base URLs verified
# from official docs 2026-07. Key-gated in run().
#
# Thinking parity: every open model is registered as reasoning-capable and run
# with `--thinking medium`. Per-model compat maps that to the closest vendor
# thinking-on behavior: GLM-5.2 medium -> Z.ai `reasoning_effort=high`; DeepSeek,
# Kimi, and GLM-4.7 Flash use the vendor's thinking-on default (no medium level).
# (Duplicated across pi/opencode/codex so each adapter stays self-contained.)
OPEN_MODELS = {
    "glm-5.2":           {"provider": "zai",      "model_id": "glm-5.2",           "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": True, "thinkingFormat": "zai"},      "thinkingLevelMap": {"minimal": None, "low": "high", "medium": "high", "high": "high", "xhigh": "max"}},
    "glm-4.7-flash":     {"provider": "zai",      "model_id": "glm-4.7-flash",     "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": False, "thinkingFormat": "zai"},     "thinkingLevelMap": {"off": None}},
    "deepseek-v4-flash": {"provider": "deepseek", "model_id": "deepseek-v4-flash", "base_url": "https://api.deepseek.com",     "env_key": "DEEPSEEK_API_KEY", "display": "DeepSeek",      "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": False, "thinkingFormat": "deepseek", "requiresReasoningContentOnAssistantMessages": True}, "thinkingLevelMap": {"off": None}},
    "kimi-k2.7-code":    {"provider": "moonshot", "model_id": "kimi-k2.7-code",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi", "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": False, "maxTokensField": "max_tokens", "supportsStrictMode": False, "thinkingFormat": "deepseek"}, "thinkingLevelMap": {"off": None}},
}


def _unsupported(model):
    known = list(MODELS) + list(OPEN_MODELS)
    return {"completed": False, "error": f"unsupported-model: {model!r} (have {known})",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def _setup_needed(env_key, model):
    return {"completed": False,
            "error": f"SETUP-NEEDED: export {env_key} to use {model}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def _pi_provider_ext(spec):
    """JS extension source registering the open provider (loaded via -e).

    pi resolves "$ENV_KEY" in apiKey from the environment; api
    "openai-completions" appends /chat/completions to baseUrl. The model
    metadata advertises reasoning plus vendor-specific thinking controls so the
    CLI's `--thinking medium` becomes a real thinking-on request.
    """
    return (
        "export default function (pi) {\n"
        f'  pi.registerProvider("{spec["provider"]}", {{\n'
        f'    name: "{spec["display"]}",\n'
        f'    baseUrl: "{spec["base_url"]}",\n'
        f'    apiKey: "${spec["env_key"]}",\n'
        '    api: "openai-completions",\n'
        "    models: [{\n"
        f'      id: "{spec["model_id"]}", name: "{spec["model_id"]}",\n'
        "      reasoning: true, input: [\"text\"],\n"
        f'      compat: {json.dumps(spec["compat"])},\n'
        f'      thinkingLevelMap: {json.dumps(spec["thinkingLevelMap"])},\n'
        "      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },\n"
        "      contextWindow: 128000, maxTokens: 8192\n"
        "    }]\n"
        "  });\n"
        "}\n"
    )


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
    if model in MODELS:
        if not os.path.exists(_REAL_AUTH):
            return {
                "completed": False,
                "error": f"missing pi auth at {_REAL_AUTH}",
                "output_tail": "",
                "tokens": None,
                "turns": None,
                "cmd": None,
            }
    elif model in OPEN_MODELS:
        spec = OPEN_MODELS[model]
        if not os.environ.get(spec["env_key"]):
            return _setup_needed(spec["env_key"], model)
    else:
        return _unsupported(model)

    iso_home = tempfile.mkdtemp(prefix="pi_home_")
    try:
        env = dict(os.environ)
        env["HOME"] = iso_home

        if model in MODELS:
            # Subscription route: isolate HOME with only the copied auth.json.
            agent_dir = os.path.join(iso_home, ".pi", "agent")
            os.makedirs(agent_dir, exist_ok=True)
            shutil.copy2(_REAL_AUTH, os.path.join(agent_dir, "auth.json"))
            cmd = [
                "pi", "-p",
                "--no-extensions",
                "--provider", _PROVIDER,
                "--model", MODELS[model],
                "--thinking", _THINKING[model],
                "--mode", "json",
                instruction,
            ]
        else:
            # Open model: register the provider via a temp extension (env key
            # supplies auth). No subscription auth.json needed.
            spec = OPEN_MODELS[model]
            ext_path = os.path.join(iso_home, "open-provider.mjs")
            with open(ext_path, "w", encoding="utf-8") as fh:
                fh.write(_pi_provider_ext(spec))
            cmd = [
                "pi", "-p",
                "--no-extensions",
                "-e", ext_path,
                "--provider", spec["provider"],
                "--model", spec["model_id"],
                "--thinking", spec["thinking"],
                "--mode", "json",
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
                env=env,
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
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
