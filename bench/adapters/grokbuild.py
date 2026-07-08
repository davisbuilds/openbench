"""Adapter for xAI's `grok` (Grok Build CLI) running OPEN models via BYOK.

Headless invocation:
    HOME=<isolated tmp with generated ~/.grok/config.toml>
    grok --no-auto-update -p <instruction> --model <canonical-open-model> \
         --output-format streaming-json --always-approve --no-plan \
         --no-subagents --disable-web-search --no-memory --cwd <workdir>

Custom models are declared in the isolated HOME only; the user's real ~/.grok is
never read, copied, or mounted.  The config uses Grok's documented user config
shape:

    [model.<name>]
    model = "vendor-model-id"
    base_url = "https://vendor.example/v1"
    name = "Display Name"
    env_key = "VENDOR_API_KEY"

    [models]
    default = "<name>"

Probe result (2026-07-07): this BYOK custom-model path works without xAI login.
`--output-format streaming-json` emits JSONL events like
``thought``/``text``/``end``.  Those events carried no token-usage fields in the
observed stream, so token accounting also reads Grok's local
``logs/unified.jsonl`` ``shell.turn.inference_done`` counters from the isolated
HOME.  Turns are counted from terminal ``end`` events (single `-p` run => 1).
"""

import json
import os
import shutil
import subprocess
import tempfile

NAME = "grokbuild"
_EXE = "grok"

# Required by ADAPTER_SPEC / doctor.py. This adapter is open-model-only for now.
MODELS = {}

# Exact OpenAI-compatible endpoint data copied from bench/adapters/pi.py.
OPEN_MODELS = {
    "glm-5.2":           {"model_id": "glm-5.2",           "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM"},
    "deepseek-v4-flash": {"model_id": "deepseek-v4-flash", "base_url": "https://api.deepseek.com",     "env_key": "DEEPSEEK_API_KEY", "display": "DeepSeek"},
    "kimi-k2.7-code":    {"model_id": "kimi-k2.7-code",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi"},
}


def _unsupported(model):
    return {"completed": False,
            "error": f"unsupported-model: {model!r} (have {list(OPEN_MODELS)})",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def _setup_needed(msg):
    return {"completed": False, "error": f"SETUP-NEEDED: {msg}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def _resolve_exe():
    return shutil.which(_EXE)


def version():
    """Return the Grok CLI version string (with binary path), or None."""
    exe = _resolve_exe()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - version probing must never raise
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return f"{out} ({exe})" if out else None


def _toml_str(value):
    # JSON string syntax is valid TOML basic-string syntax for these values.
    return json.dumps(str(value))


def _config_toml(model, spec):
    return (
        "[cli]\n"
        "auto_update = false\n\n"
        "[models]\n"
        f"default = {_toml_str(model)}\n"
        f"web_search = {_toml_str(model)}\n\n"
        f"[model.{_toml_str(model)}]\n"
        f"model = {_toml_str(spec['model_id'])}\n"
        f"base_url = {_toml_str(spec['base_url'])}\n"
        f"name = {_toml_str(spec['display'])}\n"
        f"env_key = {_toml_str(spec['env_key'])}\n"
        'api_backend = "chat_completions"\n'
        "stream_tool_calls = false\n"
        "context_window = 128000\n"
        "max_completion_tokens = 8192\n\n"
        "[session]\n"
        "save_on_end = false\n\n"
        "[memory]\n"
        "enabled = false\n\n"
        "[memory.session]\n"
        "save_on_end = false\n\n"
        "[subagents]\n"
        "enabled = false\n\n"
        "[compat.cursor]\n"
        "skills = false\n"
        "rules = false\n"
        "agents = false\n"
        "mcps = false\n"
        "hooks = false\n\n"
        "[compat.claude]\n"
        "skills = false\n"
        "rules = false\n"
        "agents = false\n"
        "mcps = false\n"
        "hooks = false\n"
    )


def _write_config(iso_home, model, spec):
    grok_dir = os.path.join(iso_home, ".grok")
    os.makedirs(grok_dir, exist_ok=True)
    path = os.path.join(grok_dir, "config.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_config_toml(model, spec))
    return path


def _err_tail(exc, limit=2000):
    def _dec(x):
        if x is None:
            return ""
        return x.decode("utf-8", "replace") if isinstance(x, bytes) else x
    text = _dec(exc.stdout) + _dec(exc.stderr)
    return text if limit is None else text[-limit:]


def _usage_tokens(obj):
    """Best-effort usage parser for future Grok stream shape drift."""
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage") or obj.get("tokenUsage") or obj.get("tokens")
    if not isinstance(usage, dict):
        return None
    # Known/common OpenAI/Anthropic-ish field names.  The 2026-07 probe did not
    # emit any of these, but keeping this defensive parser makes future CLI
    # additions useful without changing the adapter contract.
    total = usage.get("total_tokens") or usage.get("totalTokens")
    if isinstance(total, (int, float)):
        return int(total)
    pairs = [
        ("input_tokens", "output_tokens"),
        ("prompt_tokens", "completion_tokens"),
        ("inputTokens", "outputTokens"),
    ]
    for a, b in pairs:
        if isinstance(usage.get(a), (int, float)) and isinstance(usage.get(b), (int, float)):
            return int(usage[a]) + int(usage[b])
    return None


def _parse_log_usage(grok_dir):
    """Return fresh token count from Grok's local run log, if present.

    The observed streaming-json events do not carry usage, but Grok writes a
    final ``shell.turn.inference_done`` log with prompt/completion counters.
    Treat cached prompt tokens like other adapters do: cache reads are excluded
    from the fresh-token total.
    """
    log_path = os.path.join(grok_dir, "logs", "unified.jsonl")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("msg") != "shell.turn.inference_done":
            continue
        ctx = obj.get("ctx") if isinstance(obj.get("ctx"), dict) else {}
        prompt = ctx.get("prompt_tokens")
        cached = ctx.get("cached_prompt_tokens") or 0
        completion = ctx.get("completion_tokens")
        if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
            return max(0, int(prompt) - int(cached)) + int(completion)
    return None


def _parse_stream(stdout):
    """Parse Grok `streaming-json` stdout into (tokens, turns, tail)."""
    text_parts = []
    tokens_total = 0
    found_tokens = False
    end_events = 0
    parsed_any = False

    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        parsed_any = True
        typ = obj.get("type")
        if typ == "text" and obj.get("data"):
            text_parts.append(str(obj["data"]))
        elif typ in {"message", "assistant"} and obj.get("text"):
            text_parts.append(str(obj["text"]))
        if typ == "end":
            end_events += 1
        tok = _usage_tokens(obj)
        if tok is not None:
            tokens_total += tok
            found_tokens = True

    tail = "".join(text_parts)[-2000:]
    tokens = tokens_total if found_tokens else None
    turns = end_events or (1 if parsed_any else None)
    return tokens, turns, tail


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model not in OPEN_MODELS:
        return _unsupported(model)
    spec = OPEN_MODELS[model]
    if not os.environ.get(spec["env_key"]):
        return _setup_needed(f"export {spec['env_key']} to use {model}")
    exe = _resolve_exe()
    if not exe:
        return _setup_needed("install Grok Build CLI (`npm install -g @xai-official/grok`) and ensure `grok` is on PATH")

    iso_home = tempfile.mkdtemp(prefix="grokbuild_home_")
    try:
        grok_dir = os.path.dirname(_write_config(iso_home, model, spec))
        env = dict(os.environ)
        env["HOME"] = iso_home
        # Keep Grok's generated state within the disposable home and suppress
        # non-essential network work where the CLI exposes a switch.
        cmd = [
            exe,
            "--no-auto-update",
            "-p", instruction,
            "--model", model,
            "--output-format", "streaming-json",
            "--always-approve",
            "--no-plan",
            "--no-subagents",
            "--disable-web-search",
            "--no-memory",
            "--cwd", workdir,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, capture_output=True, text=True,
                timeout=timeout_s, stdin=subprocess.DEVNULL, env=env,
            )
        except subprocess.TimeoutExpired as e:
            full_output = _err_tail(e, limit=None)
            return {"completed": False, "error": f"timeout after {timeout_s}s",
                    "output_tail": full_output[-2000:], "full_output": full_output,
                    "tokens": None, "turns": None, "cmd": cmd}

        combined = (proc.stdout or "") + (proc.stderr or "")
        try:
            tokens, turns, tail = _parse_stream(proc.stdout or "")
            if tokens is None:
                tokens = _parse_log_usage(grok_dir)
        except Exception:  # noqa: BLE001 - parsing must not break a run
            tokens, turns, tail = None, None, ""
        if not tail:
            tail = combined[-2000:]
        return {"completed": proc.returncode == 0,
                "error": None if proc.returncode == 0 else f"exit {proc.returncode}",
                "output_tail": tail, "full_output": combined,
                "tokens": tokens, "turns": turns, "cmd": cmd}
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
