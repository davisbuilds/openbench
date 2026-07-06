"""Adapter for the `codex` CLI (OpenAI Codex, ChatGPT-subscription login).

Headless invocation:
    codex exec --json --skip-git-repo-check -C <workdir> \
        -s workspace-write \
        -m gpt-5.5 -c model_reasoning_effort="medium" <instruction>

Notes / quirks:
- `codex exec` is fully non-interactive; there are no approval prompts to
  suppress. `-s workspace-write` is the least-privileged sandbox that still
  lets the agent edit files inside the workspace root. In the docker lane
  (BENCH_IN_CONTAINER=1) it is replaced by
  `--dangerously-bypass-approvals-and-sandbox`: bwrap cannot nest inside the
  bench container, and the disposable container is the external sandbox.
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

Open models (M4): DeepSeek / Z.ai / Moonshot are chat-only, but codex 0.142
requires the Responses API, so they run through a host-side LiteLLM bridge (see
OPEN_MODELS and bench/openmodel_bridge.sh). Token accounting is UNCHANGED in
mechanism (same fresh-basis parse of `turn.completed.usage`), but the basis now
transits the bridge: the counts codex reports are the bridge's Responses-shaped
usage, remapped from the upstream chat-completions `usage` (prompt_tokens ->
input_tokens, completion_tokens -> output_tokens, split reasoning_tokens). This
matches the vendor's own billed usage; there is no codex-native usage for these
models to cross-check against.
"""

import json
import os
import shutil
import socket
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

# --- M4 open models (first-party pay-per-token, chat-only vendors) -----------
# codex-cli >=0.142 REMOVED wire_api="chat"; custom providers must speak the
# Responses API. DeepSeek / Z.ai / Moonshot only serve /chat/completions, so
# codex cannot talk to them directly. We route through a host-side LiteLLM proxy
# (the "bridge", see bench/openmodel_bridge.sh) that accepts /v1/responses
# ingress and translates each call to /chat/completions upstream. codex is thus
# pointed at the bridge (wire_api stays "responses"); ``base_url`` is the bridge,
# NOT the vendor. The bridge maps model_id -> the vendor route + real key.
#
# BRIDGE REQUIREMENT: a human/orchestrator must start the bridge BEFORE any
# open-model codex run (`bench/openmodel_bridge.sh`, foreground). The runner does
# NOT manage it. run() does a cheap TCP probe first and returns SETUP-NEEDED if
# the bridge port is unreachable.
#
# env_key is still required (SETUP-NEEDED if unset): codex refuses to start a
# custom provider whose env_key names an unset variable, and it sends that value
# as the ingress bearer (the bridge ignores it and injects the vendor key from
# its own environment).
#
# Base URLs below are documentation/provenance only; the adapter talks to the
# bridge, which is configured with these same vendor endpoints.
#
# Thinking parity: the adapter requests `model_reasoning_effort="medium"` for
# every open model. The LiteLLM bridge hook normalizes that to the closest
# vendor thinking-on setting (GLM-5.2 medium -> Z.ai high; otherwise the
# vendor's thinking-on default when levels are not exposed on the bridge route).
# (Duplicated across the pi/opencode/codex adapters so each stays self-contained
#  under the runner's isolated importer.)
OPEN_MODELS = {
    "glm-5.2":           {"provider": "zai",      "model_id": "glm-5.2",           "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "effort": "medium"},
    "glm-4.7-flash":     {"provider": "zai",      "model_id": "glm-4.7-flash",     "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "effort": "medium"},
    "deepseek-v4-flash": {"provider": "deepseek", "model_id": "deepseek-v4-flash", "base_url": "https://api.deepseek.com",     "env_key": "DEEPSEEK_API_KEY", "display": "DeepSeek",      "effort": "medium"},
    "kimi-k2.7-code":    {"provider": "moonshot", "model_id": "kimi-k2.7-code",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi", "effort": "medium"},
}

# Host-side bridge (LiteLLM proxy). Port must match bench/openmodel_bridge.sh
# (both default to 4141; override in lockstep via BENCH_BRIDGE_PORT).
_BRIDGE_DEFAULT_PORT = 4141


def _bridge_host():
    """Hostname the bridge is reachable at from the current lane.

    In the docker lane entry.py sets BENCH_IN_CONTAINER; Docker Desktop resolves
    ``host.docker.internal`` to the host running the bridge. On the host lane it
    is plain ``localhost``.
    """
    return "host.docker.internal" if os.environ.get("BENCH_IN_CONTAINER") else "localhost"


def _bridge_port():
    return int(os.environ.get("BENCH_BRIDGE_PORT", _BRIDGE_DEFAULT_PORT))


def _bridge_base_url():
    """codex ``base_url`` for the bridge; codex appends ``/responses`` to it."""
    return f"http://{_bridge_host()}:{_bridge_port()}/v1"


def _bridge_reachable(timeout=3.0):
    """Cheap TCP connect probe to the bridge port. True iff something accepts."""
    try:
        with socket.create_connection((_bridge_host(), _bridge_port()), timeout=timeout):
            return True
    except OSError:
        return False


def _bridge_down(model):
    return {"completed": False,
            "error": (f"SETUP-NEEDED: open-model bridge unreachable at "
                      f"{_bridge_host()}:{_bridge_port()} for {model} "
                      f"(start it: bench/openmodel_bridge.sh)"),
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


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
    if os.environ.get("BENCH_IN_CONTAINER"):
        # codex's own sandbox (bwrap) needs user namespaces and cannot nest
        # inside the bench container; the disposable container IS the external
        # sandbox, which is the documented intent of this flag.
        sandbox = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        sandbox = ["-s", "workspace-write"]
    base = [
        "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "-C", workdir,
    ] + sandbox
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
        # Route through the host-side Responses<->Chat bridge (see the OPEN_MODELS
        # docstring and bench/openmodel_bridge.sh). Fail fast with a clear
        # SETUP-NEEDED error if the bridge isn't up, rather than letting codex
        # spend a turn's timeout failing to connect.
        if not _bridge_reachable():
            return _bridge_down(model)
        prov = spec["provider"]
        cmd = base + [
            "-c", f'model_providers.{prov}.name="{spec["display"]}"',
            "-c", f'model_providers.{prov}.base_url="{_bridge_base_url()}"',
            "-c", f'model_providers.{prov}.env_key="{spec["env_key"]}"',
            "-c", f'model_providers.{prov}.wire_api="responses"',
            "-c", f'model_provider="{prov}"',
            "-c", f'model_reasoning_effort="{spec["effort"]}"',
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
        # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr so the
        # runner can persist a complete local transcript. Cheap here (already
        # concatenated). LOCAL-ONLY: transcripts are never published unscrubbed.
        "full_output": combined,
        "tokens": tokens,
        "turns": turns,
        "cmd": cmd,
    }
