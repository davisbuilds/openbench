"""Adapter for the `pi` CLI (ChatGPT-subscription "openai-codex" route).

Headless invocation:
    HOME=<isolated tmp with only .pi/agent/auth.json>
    pi -p --provider openai-codex --model gpt-5.5 \
       --thinking medium <instruction>

Notes / quirks:
- pi loads the user's personal extensions from the real ~/.pi. One of them
  (pi-goal) crashes `-p` non-interactive mode. To avoid this WITHOUT touching
  the user's config, we run pi under an ISOLATED HOME: a fresh temp dir that
  contains ONLY `.pi/agent/auth.json` copied from the real one. No settings.json
  means no personal extensions are registered; built-in factory behavior remains.
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
from urllib.parse import urlsplit

try:
    from obench.auth_persist import try_persist_auth_file
except ImportError:  # file-path / Docker mount layout
    from auth_persist import try_persist_auth_file

NAME = "pi"


def _doctor_auth(probes):
    """Doctor AUTH probe: isolated-HOME route needs ~/.pi/agent/auth.json + openai-codex."""
    path = "~/.pi/agent/auth.json"
    if not probes.exists(path):
        return False, f"missing {os.path.expanduser(path)}"
    data = probes.read_json(path)
    if not isinstance(data, dict):
        return False, f"unreadable JSON at {os.path.expanduser(path)}"
    if "openai-codex" in data:
        return True, "entry: openai-codex"
    return False, "no openai-codex entry in ~/.pi/agent/auth.json"


# Optional doctor metadata: scanned by obench.doctor to build the harness
# preflight table without hard-coding every adapter in doctor.py.
DOCTOR = {"cli": "pi", "auth": _doctor_auth}

# canonical model name -> pi provider/model pair. Both routes use pi's
# subscription/OAuth credentials under ~/.pi; no API key is required here.
# Thinking parity for the opus frontier lane: Anthropic Claude Opus 4.8 is run
# with pi's `--thinking medium`, matching the benchmark's medium-reasoning tier.
MODELS = {
    "gpt-5.5-medium": {"provider": "openai-codex", "model_id": "gpt-5.5", "thinking": "medium"},
    "gpt-5.6-sol": {"provider": "openai-codex", "model_id": "gpt-5.6-sol", "thinking": "medium"},
    "gpt-5.6-terra": {"provider": "openai-codex", "model_id": "gpt-5.6-terra", "thinking": "medium"},
    "gpt-5.6-luna": {"provider": "openai-codex", "model_id": "gpt-5.6-luna", "thinking": "medium"},
    "claude-opus-4-8": {"provider": "anthropic", "model_id": "claude-opus-4-8", "thinking": "medium"},
    "grok-4.5": {"provider": "xai", "model_id": "grok-4.5", "thinking": "medium"},
}
_REAL_AUTH = os.path.expanduser("~/.pi/agent/auth.json")
_EXE = "pi"


def _empty_token_usage():
    return {
        "tokens_input_uncached": None,
        "tokens_cache_read": None,
        "tokens_cache_write": None,
        "tokens_output": None,
        "tokens_reasoning": None,
        "usage_raw": None,
        "token_basis": None,
    }


def _legacy_tokens(token_usage):
    # Delegated TOKEN_PARITY contract: keep the legacy scalar as
    # uncached_input + output. Cache reads and cache writes remain available in
    # split fields but are intentionally not folded into this compatibility
    # value.
    inp = token_usage.get("tokens_input_uncached")
    out = token_usage.get("tokens_output")
    if isinstance(inp, int) and isinstance(out, int):
        return inp + out
    return None


def _num(value):
    return int(value) if isinstance(value, (int, float)) else None


def _proxy_cell_url(*parts):
    base = os.environ.get("OPENBENCH_PROXY_BASE_URL")
    token = os.environ.get("OPENBENCH_PROXY_CELL_TOKEN")
    if not os.environ.get("OPENBENCH_PROXY") or not base or not token:
        return None
    path = "/".join(str(p).strip("/") for p in ("cell", token, *parts) if str(p).strip("/"))
    return base.rstrip("/") + "/" + path


def _proxied_base_url(route, original_url=None):
    if not os.environ.get("OPENBENCH_PROXY"):
        return original_url
    if route == "codex":
        return _proxy_cell_url("codex", "backend-api")
    # AI gateways / routers meter on the proxy's dedicated gateway/<name> route;
    # direct providers use chat/<vendor>. The gateway upstream registry
    # (proxy.DEFAULT_GATEWAY_UPSTREAMS) already carries the full base path
    # (e.g. .../api/v1), so we must NOT also append the model base_url's path
    # tail here — doing so doubles it (.../api/v1/api/v1/...). pi's
    # openai-completions api appends /chat/completions to whatever base we return.
    if route in GATEWAY_PROVIDERS:
        return _proxy_cell_url("gateway", route)
    parsed = urlsplit(original_url or "")
    tail = (parsed.path or "").strip("/")
    vendor = route
    return _proxy_cell_url("chat", vendor, tail)


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


_DELTA_MARKER = '"type":"message_update"'


def _run_streaming(cmd, cwd, timeout_s, env):
    """Run pi consuming stdout line-by-line, dropping per-token delta events.

    ``--mode json`` re-emits the FULL accumulated partial message inside every
    ``message_update`` delta event, so a single long reasoning turn produces
    output quadratic in its token count (observed: GBs on 32k-token turns,
    OOM-killing the container). The parser only needs ``turn_end``/``agent_end``
    events, which carry final content and usage — so delta lines are discarded
    at read time instead of buffered.

    Returns (stdout_text, stderr_text, returncode, timed_out).
    """
    import threading

    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, stdin=subprocess.DEVNULL, env=env,
    )
    out_lines, err_chunks = [], []

    def _drain_stdout():
        for line in proc.stdout:
            if _DELTA_MARKER not in line:
                out_lines.append(line)
        proc.stdout.close()

    def _drain_stderr():
        for chunk in proc.stderr:
            err_chunks.append(chunk)
        proc.stderr.close()

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    t_out.join(timeout=10)
    t_err.join(timeout=10)
    return "".join(out_lines), "".join(err_chunks), proc.returncode, timed_out


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
    "kimi-k3":    {"provider": "moonshot", "model_id": "kimi-k3",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi K3", "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": False, "maxTokensField": "max_tokens", "supportsStrictMode": False, "thinkingFormat": "deepseek"}, "thinkingLevelMap": {"off": None}},
    "laguna-s-2.1": {"provider": "openrouter", "context_window": 262144, "max_tokens": 32768, "model_id": "poolside/laguna-s-2.1", "base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY", "display": "OpenRouter Poolside Laguna S 2.1", "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": True, "supportsStrictMode": False}, "thinkingLevelMap": {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"}},
    "inkling": {"provider": "openrouter", "context_window": 524288, "max_tokens": 32768, "model_id": "thinkingmachines/inkling", "base_url": "https://openrouter.ai/api/v1", "env_key": "OPENROUTER_API_KEY", "display": "OpenRouter Thinking Machines Inkling", "thinking": "medium", "compat": {"supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": True, "supportsStrictMode": False}, "thinkingLevelMap": {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"}},
}


# --- Gateway arms (gateway / model-router benchmarking) ---------------------
# A gateway is one OpenAI-compatible endpoint fronting many providers, so a
# single gateway key reaches models from different vendors -- e.g. an OpenAI and
# an Anthropic model in the same run without per-vendor auth. Each gateway's
# baseUrl is metered through the counting proxy's gateway/<name> route
# (obench/proxy.py DEFAULT_GATEWAY_UPSTREAMS) rather than the direct-provider
# chat/<vendor> route; GATEWAY_PROVIDERS lists the provider names that route that
# way. All three below are OpenAI-compatible (swap baseUrl + key, keep the
# provider-qualified model slug).
GATEWAYS = {
    "openrouter": {"display": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "env_key": "OPENROUTER_API_KEY"},
    "vercel": {"display": "Vercel AI Gateway", "base_url": "https://ai-gateway.vercel.sh/v1",
               "env_key": "AI_GATEWAY_API_KEY"},
    "concentrate": {"display": "Concentrate.ai", "base_url": "https://api.concentrate.ai/v1",
                    "env_key": "CONCENTRATE_API_KEY"},
}
GATEWAY_PROVIDERS = set(GATEWAYS)

# Provider-qualified model slugs offered behind every gateway (all three accept
# the OpenAI/Anthropic-style creator/model form). Context windows are per model.
_GATEWAY_MODEL_SLUGS = {
    "openai/gpt-5.6": {"context_window": 400000, "max_tokens": 32768},
    "anthropic/claude-sonnet-4.5": {"context_window": 200000, "max_tokens": 32768},
}
_GATEWAY_COMPAT = {"supportsStore": False, "supportsDeveloperRole": False,
                   "supportsReasoningEffort": True, "supportsStrictMode": False}
_GATEWAY_THINKING_MAP = {"minimal": "minimal", "low": "low", "medium": "medium",
                         "high": "high", "xhigh": "high"}


def _build_gateway_models():
    """Fixed-model arms: canonical name ``<gateway>/<provider>/<model>``.

    FIXED-MODEL mode: exactly one model, no router fallback (add provider routing
    controls later for a router-mode arm). Registered via the same provider
    extension as OPEN_MODELS; only the proxy route differs (GATEWAY_PROVIDERS).
    """
    models = {}
    for gw_name, gw in GATEWAYS.items():
        for slug, dims in _GATEWAY_MODEL_SLUGS.items():
            models[f"{gw_name}/{slug}"] = {
                "provider": gw_name, "model_id": slug,
                "base_url": gw["base_url"], "env_key": gw["env_key"],
                "display": gw["display"], "thinking": "medium",
                "context_window": dims["context_window"], "max_tokens": dims["max_tokens"],
                "compat": _GATEWAY_COMPAT, "thinkingLevelMap": _GATEWAY_THINKING_MAP,
            }
    return models


GATEWAY_MODELS = _build_gateway_models()


def _open_model_spec(model):
    """Return the extension-registered spec for an open or gateway model."""
    return GATEWAY_MODELS.get(model) or OPEN_MODELS.get(model)


def _unsupported(model):
    known = list(MODELS) + list(OPEN_MODELS) + list(GATEWAY_MODELS)
    return {"completed": False, "error": f"unsupported-model: {model!r} (have {known})",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}


def _setup_needed(env_key, model):
    return {"completed": False,
            "error": f"SETUP-NEEDED: export {env_key} to use {model}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}


def _subscription_setup_needed(provider, model):
    return {"completed": False,
            "error": (f"SETUP-NEEDED: login to pi provider {provider!r} for {model} "
                      f"(missing provider credential in {_REAL_AUTH})"),
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}


def _has_subscription_auth(provider):
    try:
        with open(_REAL_AUTH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and provider in data


def _pi_models_override(base_url):
    return json.dumps({"providers": {"openai-codex": {"baseUrl": base_url}}}, indent=2)


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
        f'    baseUrl: "{_proxied_base_url(spec["provider"], spec["base_url"])}",\n'
        f'    apiKey: "${spec["env_key"]}",\n'
        '    api: "openai-completions",\n'
        "    models: [{\n"
        f'      id: "{spec["model_id"]}", name: "{spec["model_id"]}",\n'
        "      reasoning: true, input: [\"text\"],\n"
        f'      compat: {json.dumps(spec["compat"])},\n'
        f'      thinkingLevelMap: {json.dumps(spec["thinkingLevelMap"])},\n'
        "      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },\n"
        f"      contextWindow: {spec.get('context_window', 128000)}, maxTokens: {spec.get('max_tokens', 8192)}\n"
        "    }]\n"
        "  });\n"
        "}\n"
    )


def _parse_json_with_usage(stdout):
    """Parse pi's JSONL event stream into (tokens, turns, tail, token_usage).

    Split usage is summed from per-turn ``turn_end.message.usage`` records as
    verified in TOKEN_PARITY.md. ``tokens`` remains the legacy scalar:
    uncached input + output, with cache reads excluded.
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
        return None, None, "", _empty_token_usage()

    turns = sum(1 for e in events if e.get("type") == "turn_end") or None

    def _split_from_usages(usages):
        usage_raw = []
        invariant_ok = True
        totals = {
            "tokens_input_uncached": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "tokens_output": 0,
            "tokens_reasoning": 0,
        }
        for usage in usages:
            if not isinstance(usage, dict):
                continue
            inp = _num(usage.get("input"))
            cache_read = _num(usage.get("cacheRead"))
            cache_write = _num(usage.get("cacheWrite"))
            out = _num(usage.get("output"))
            reasoning = _num(usage.get("reasoning"))
            total = _num(usage.get("totalTokens"))
            if None in (inp, cache_read, cache_write, out):
                invariant_ok = False
                continue
            if total is None or inp + cache_read + cache_write + out != total:
                invariant_ok = False
            if reasoning is None or reasoning > out:
                invariant_ok = False
                reasoning = 0 if reasoning is None else reasoning
            usage_raw.append(usage)
            totals["tokens_input_uncached"] += inp
            totals["tokens_cache_read"] += cache_read
            totals["tokens_cache_write"] += cache_write
            totals["tokens_output"] += out
            totals["tokens_reasoning"] += reasoning
        if not usage_raw:
            return _empty_token_usage()
        out = _empty_token_usage()
        out.update(totals)
        out["usage_raw"] = usage_raw
        out["token_basis"] = "vendor_split" if invariant_ok else "estimated"
        return out

    token_usage = _split_from_usages(
        (ev.get("message") or {}).get("usage")
        for ev in events
        if ev.get("type") == "turn_end"
    )

    # Prefer the final agent_end message list; fall back to message_end events.
    messages = None
    for e in events:
        if e.get("type") == "agent_end" and isinstance(e.get("messages"), list):
            messages = e["messages"]
    if messages is None:
        messages = [e.get("message") for e in events
                    if e.get("type") == "message_end"]

    if token_usage.get("token_basis") is None:
        # Older/documented pi JSON shapes put usage on assistant messages in
        # agent_end/message_end rather than on turn_end.message. Keep that
        # surface as a fallback, but prefer turn_end to avoid double-counting
        # when both are present.
        token_usage = _split_from_usages(
            msg.get("usage")
            for msg in messages
            if isinstance(msg, dict) and msg.get("role") == "assistant"
        )

    transcript = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for part in (msg.get("content") or []):
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                transcript.append(part["text"])

    tail = "\n".join(transcript)[-2000:]
    return _legacy_tokens(token_usage), turns, tail, token_usage



def _parse_json(stdout):
    """Backward-compatible parser returning legacy fields only."""
    tokens, turns, tail, token_usage = _parse_json_with_usage(stdout)
    return tokens, turns, tail

def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model in MODELS:
        provider = MODELS[model]["provider"]
        if not _has_subscription_auth(provider):
            return _subscription_setup_needed(provider, model)
    elif _open_model_spec(model) is not None:
        spec = _open_model_spec(model)
        if not os.environ.get(spec["env_key"]):
            return _setup_needed(spec["env_key"], model)
    else:
        return _unsupported(model)

    iso_home = tempfile.mkdtemp(prefix="pi_home_")
    isolated_auth = None
    try:
        env = dict(os.environ)
        env["HOME"] = iso_home
        # PI_CODING_AGENT_DIR overrides HOME; always replace an inherited owner
        # value so settings/resources/auth cannot escape the isolated tree.
        env["PI_CODING_AGENT_DIR"] = os.path.join(iso_home, ".pi", "agent")
        env.pop("PI_CODING_AGENT_SESSION_DIR", None)
        env.pop("PI_PACKAGE_DIR", None)

        if model in MODELS:
            # Subscription route: isolate HOME with only the copied auth.json.
            spec = MODELS[model]
            agent_dir = os.path.join(iso_home, ".pi", "agent")
            os.makedirs(agent_dir, exist_ok=True)
            isolated_auth = os.path.join(agent_dir, "auth.json")
            shutil.copy2(_REAL_AUTH, isolated_auth)
            proxy_url = _proxied_base_url("codex")
            if proxy_url:
                with open(os.path.join(agent_dir, "models.json"), "w", encoding="utf-8") as fh:
                    fh.write(_pi_models_override(proxy_url))
            cmd = [
                "pi", "-p",
                # Benchmark workspaces are data, not executable configuration.
                # This preserves Pi's built-in factory tools while preventing a
                # task's .pi extensions/packages from running in the harness.
                "--no-approve",
                "--provider", spec["provider"],
                "--model", spec["model_id"],
                "--thinking", spec["thinking"],
                "--mode", "json",
                instruction,
            ]
        else:
            # Open / gateway model: register the provider via a temp extension
            # (env key supplies auth). No subscription auth.json needed.
            spec = _open_model_spec(model)
            ext_path = os.path.join(iso_home, "open-provider.mjs")
            with open(ext_path, "w", encoding="utf-8") as fh:
                fh.write(_pi_provider_ext(spec))
            cmd = [
                "pi", "-p",
                "--no-approve",
                "-e", ext_path,
                "--provider", spec["provider"],
                "--model", spec["model_id"],
                "--thinking", spec["thinking"],
                "--mode", "json",
                instruction,
            ]

        stdout_text, stderr_text, returncode, timed_out = _run_streaming(
            cmd, workdir, timeout_s, env)
        if timed_out:
            full_output = stdout_text + stderr_text
            return {
                "completed": False,
                "error": f"timeout after {timeout_s}s",
                "output_tail": full_output[-2000:],
                "full_output": full_output,
                "tokens": None,
                "turns": None,
                "cmd": cmd,
                **_empty_token_usage(),
            }

        combined = stdout_text + stderr_text
        try:
            tokens, turns, tail, token_usage = _parse_json_with_usage(stdout_text)
        except Exception:  # noqa: BLE001 - never let usage parsing break a run
            tokens, turns, tail, token_usage = None, None, "", _empty_token_usage()
        if not tail:
            tail = combined[-2000:]

        return {
            "completed": returncode == 0,
            "error": None if returncode == 0 else f"exit {returncode}",
            "output_tail": tail,
            # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr for the
            # runner's local transcript. LOCAL-ONLY; never published unscrubbed.
            "full_output": combined,
            "tokens": tokens,
            "turns": turns,
            "cmd": cmd,
            **token_usage,
        }
    finally:
        if isolated_auth is not None:
            try_persist_auth_file(isolated_auth, _REAL_AUTH)
        shutil.rmtree(iso_home, ignore_errors=True)
