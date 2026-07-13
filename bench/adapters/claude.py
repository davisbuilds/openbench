"""Adapter for the `claude` CLI (Claude Code) running OPEN models.

This adapter supports open models served over vendors' Anthropic-compatible
endpoints, plus an explicit first-party Anthropic API-key frontier route for
`claude-opus-4-8`. It never uses Claude Code OAuth or mounts ~/.claude; the
frontier route is gated only on `ANTHROPIC_API_KEY`.

Headless invocation (per open model):
    HOME=<isolated tmp>  CLAUDE_CONFIG_DIR=<iso>/.claude
    ANTHROPIC_BASE_URL=<vendor anthropic endpoint>
    ANTHROPIC_API_KEY=<vendor key>            # sent as the x-api-key header
    claude -p --bare --output-format json --model <vendor model id> \
           --effort medium --dangerously-skip-permissions \
           --no-session-persistence <instruction>

Billing-safety (why open models can never touch the Anthropic subscription):
- ``ANTHROPIC_BASE_URL`` physically points every request at the vendor host, so
  api.anthropic.com (where the subscription lives) is never contacted.
- ``--bare`` makes the CLI read auth STRICTLY from ``ANTHROPIC_API_KEY`` and
  never from OAuth or the macOS keychain (verified in `claude --help`: "OAuth
  and keychain are never read"). It also skips hooks / LSP / plugins / auto-
  memory / CLAUDE.md auto-discovery, giving a clean, reproducible agent (the
  analogue of pi's ``--no-extensions`` + isolated HOME).
- An isolated ``HOME`` + ``CLAUDE_CONFIG_DIR`` (fresh temp dir) means the user's
  real ``~/.claude`` (config, ``.credentials.json``, sessions) is never read or
  written. The temp dir is removed after the run.
- The child env is sanitized (``_clean_env``): every pre-existing ``ANTHROPIC_*``
  is dropped before our two are set (so a stray key/token can't shadow the vendor
  key), and nested-session vars (``CLAUDECODE`` / ``CLAUDE_CODE_*`` / ``CMUX_*`` /
  ``NODE_OPTIONS``) are stripped so the child runs as a clean top-level process
  rather than a bridged sub-session. ``_resolve_exe`` likewise skips any
  ``cmux``/``shim`` PATH entry. Both are no-ops in a normal shell / the docker
  image and matter only when the runner is launched from inside Claude Code.

Non-interactive:
- ``-p`` prints one response and exits (also skips the workspace-trust dialog).
- ``--dangerously-skip-permissions`` suppresses every tool-permission prompt.
  Safe here: the runner hands us a disposable workspace copy (docker lane) or a
  throwaway temp dir (local lane); the agent's edits are meant to be transient.
- ``--no-session-persistence`` avoids writing session logs to disk.

Output / token accounting:
- ``--output-format json`` prints a SINGLE result object with ``num_turns``,
  ``is_error``, ``result`` (final assistant text), a top-level ``usage`` and a
  per-model ``modelUsage`` map. See ``_parse_json``.
- Token accounting emits TOKEN_PARITY.md split fields. Anthropic-style
  input/cache fields are disjoint and cumulative ``modelUsage`` is preferred:
      tokens_input_uncached = inputTokens
      tokens_cache_read     = cacheReadInputTokens
      tokens_cache_write    = cacheCreationInputTokens
      tokens_output         = outputTokens
      tokens_reasoning      = None  # not exposed by Claude Code JSON today
      tokens                = tokens_input_uncached + tokens_output
  Falls back to top-level ``usage`` when ``modelUsage`` is absent. turns =
  ``num_turns``. Parsing is defensive: shape drift yields None fields + raw tail.
"""

import json
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

NAME = "claude"
_EXE = "claude"


def _proxy_cell_url(*parts):
    base = os.environ.get("OPENBENCH_PROXY_BASE_URL")
    token = os.environ.get("OPENBENCH_PROXY_CELL_TOKEN")
    if not os.environ.get("OPENBENCH_PROXY") or not base or not token:
        return None
    path = "/".join(str(p).strip("/") for p in ("cell", token, *parts) if str(p).strip("/"))
    return base.rstrip("/") + "/" + path


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


def _num(value, default=None):
    if isinstance(value, (int, float)):
        return int(value)
    return default

# First-party Anthropic API-key route. Thinking parity for the opus frontier
# lane: Claude Code gets `--effort medium`, matching the medium-reasoning tier.
# Auth is plain ANTHROPIC_API_KEY only; ~/.claude remains unmounted/unused.
MODELS = {
    "claude-opus-4-8": {"model_id": "claude-opus-4-8", "env_key": "ANTHROPIC_API_KEY", "effort": "medium"},
}

# --- Open models via each vendor's Anthropic-compatible endpoint -------------
# Auth: the vendor key is passed as ANTHROPIC_API_KEY (x-api-key header), which
# all three endpoints accept (verified live 2026-07-06). Model ids are the
# vendors' native ids on the /anthropic route (verified live). Key-gated in
# run() (SETUP-NEEDED dict if the env key is unset).
#
# Thinking parity: Claude Code exposes `--effort`; pass medium for every open
# model. Vendors that do not expose granular levels on their Anthropic-compatible
# route clamp this to the route's thinking-on default. Kimi uses Moonshot's
# Anthropic-compatible endpoint, not the OpenAI-compatible URL, so thinking
# blocks are enabled through Claude Code's Anthropic request shape.
OPEN_MODELS = {
    "glm-5.2":           {"model_id": "glm-5.2",           "base_url": "https://api.z.ai/api/anthropic",     "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "effort": "medium"},
    "glm-4.7-flash":     {"model_id": "glm-4.7-flash",     "base_url": "https://api.z.ai/api/anthropic",     "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "effort": "medium"},
    "deepseek-v4-flash": {"model_id": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/anthropic", "env_key": "DEEPSEEK_API_KEY", "display": "DeepSeek",      "effort": "medium"},
    "kimi-k2.7-code":    {"model_id": "kimi-k2.7-code",    "base_url": "https://api.moonshot.ai/anthropic",  "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi", "effort": "medium"},
}


def _resolve_exe():
    """Absolute path to a REAL claude binary, skipping nested-session shims.

    When the runner is itself launched from inside a Claude Code session, a
    ``cmux-cli-shims/claude`` wrapper sits first on PATH and (with ``NODE_OPTIONS``
    injection) makes a child ``claude`` run as a bridged sub-session that reports
    "Not logged in". We skip any PATH entry that looks like a shim so the child
    is a clean top-level process. subprocess resolves argv[0] against the PARENT
    PATH, not the child env's, so we must return an absolute path. Falls back to
    the bare name for normal shells and the docker image (no shims there).
    """
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        low = d.lower()
        if not d or "cmux" in low or "shim" in low:
            continue
        cand = os.path.join(d, _EXE)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return _EXE


# Env vars that make a child claude behave as a nested/bridged sub-session (set
# when the runner is launched from inside Claude Code / cmux). Stripped so the
# child runs clean. Harmless to strip in normal shells and the docker image.
def _clean_env(spec, key, iso_home):
    """Child env: strip nested-session poison, then set open-model auth only."""
    env = {}
    for k, v in os.environ.items():
        if k in ("NODE_OPTIONS", "CLAUDECODE", "AI_AGENT"):
            continue
        if k.startswith("CMUX_") or k.startswith("CLAUDE_") or k.startswith("ANTHROPIC_"):
            continue
        env[k] = v
    # Isolate config so the user's real ~/.claude is never read/written.
    env["HOME"] = iso_home
    env["CLAUDE_CONFIG_DIR"] = os.path.join(iso_home, ".claude")
    # Route open-model requests at the vendor host; first-party Anthropic uses
    # Claude Code's default Anthropic endpoint by leaving ANTHROPIC_BASE_URL
    # unset. Authenticate with the chosen API key ONLY.
    proxy_base = None
    if os.environ.get("OPENBENCH_PROXY"):
        vendor = spec.get("env_key", "").replace("_API_KEY", "").lower()
        if spec.get("base_url"):
            # Preserve vendor-specific Anthropic endpoint prefixes, e.g.
            # DeepSeek's /anthropic, while routing by a non-secret vendor name.
            tail = (urlsplit(spec["base_url"]).path or "").strip("/")
            proxy_base = _proxy_cell_url("anthropic", vendor, tail)
        else:
            proxy_base = _proxy_cell_url("anthropic")
    if proxy_base:
        env["ANTHROPIC_BASE_URL"] = proxy_base
    elif spec.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = spec["base_url"]
    env["ANTHROPIC_API_KEY"] = key
    # In the docker lane the container runs as root, and claude refuses
    # --dangerously-skip-permissions as root UNLESS it's told it's sandboxed.
    # The disposable bench container IS the external sandbox (same rationale as
    # codex's BENCH_IN_CONTAINER handling), so opt in with IS_SANDBOX=1.
    if os.environ.get("BENCH_IN_CONTAINER"):
        env["IS_SANDBOX"] = "1"
    # Hygiene: no auto-update / telemetry / non-essential calls to Anthropic.
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    env["DISABLE_BUG_COMMAND"] = "1"
    env["DISABLE_NON_ESSENTIAL_MODEL_CALLS"] = "1"
    return env


def _unsupported(model):
    known = list(MODELS) + list(OPEN_MODELS)
    return {"completed": False, "error": f"unsupported-model: {model!r} (have {known})",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}


def _setup_needed(env_key, model):
    return {"completed": False,
            "error": f"SETUP-NEEDED: export {env_key} to use {model}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}


def version():
    """Return the CLI version string (with binary path), or None on failure.

    Cheap `claude --version`; never raises (the runner calls this defensively).
    """
    exe = _resolve_exe()
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - version probing must never raise
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    if not out:
        return None
    path = exe if os.path.isabs(exe) else shutil.which(_EXE)
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


def _usage_from_model_usage(model_usage):
    """Sum Claude Code's cumulative per-model usage map, or empty usage."""
    token_usage = _empty_token_usage()
    if not isinstance(model_usage, dict):
        return token_usage
    totals = {
        "tokens_input_uncached": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "tokens_output": 0,
        "tokens_reasoning": None,
    }
    found = False
    invariant_ok = True
    for m in model_usage.values():
        if not isinstance(m, dict):
            continue
        inp = _num(m.get("inputTokens"))
        out = _num(m.get("outputTokens"))
        if inp is None or out is None:
            continue
        cache_read = _num(m.get("cacheReadInputTokens"), 0)
        cache_write = _num(m.get("cacheCreationInputTokens"), 0)
        if cache_read is None or cache_write is None or min(inp, out, cache_read, cache_write) < 0:
            invariant_ok = False
            cache_read = cache_read or 0
            cache_write = cache_write or 0
        totals["tokens_input_uncached"] += inp
        totals["tokens_cache_read"] += cache_read
        totals["tokens_cache_write"] += cache_write
        totals["tokens_output"] += out
        found = True
    if found:
        token_usage.update(totals)
        token_usage["token_basis"] = "vendor_split" if invariant_ok else "estimated"
    return token_usage


def _usage_from_top_level(usage):
    """Split usage from a top-level Anthropic-style usage dict, or empty."""
    token_usage = _empty_token_usage()
    if not isinstance(usage, dict):
        return token_usage
    inp = _num(usage.get("input_tokens"))
    out = _num(usage.get("output_tokens"))
    if inp is None or out is None:
        return token_usage
    cache_read = _num(usage.get("cache_read_input_tokens"), 0)
    cache_write = _num(usage.get("cache_creation_input_tokens"), 0)
    invariant_ok = cache_read is not None and cache_write is not None and min(inp, out, cache_read, cache_write) >= 0
    token_usage.update({
        "tokens_input_uncached": inp,
        "tokens_cache_read": cache_read or 0,
        "tokens_cache_write": cache_write or 0,
        "tokens_output": out,
        "tokens_reasoning": None,
        "token_basis": "vendor_split" if invariant_ok else "estimated",
    })
    return token_usage


def _parse_json_with_usage(stdout):
    """Parse claude's JSON result into (tokens, turns, tail, ok, token_usage).

    ``modelUsage`` is preferred because TOKEN_PARITY.md verified it is the
    cumulative run total. Claude Code does not expose reasoning tokens today, so
    ``tokens_reasoning`` is deliberately ``None``.
    """
    obj = None
    txt = (stdout or "").strip()
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        # Fall back to scanning lines for the last parseable JSON object (in
        # case any non-JSON noise leads the stream).
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                cand = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(cand, dict):
                obj = cand
                break
    if not isinstance(obj, dict):
        return None, None, "", None, _empty_token_usage()

    token_usage = _usage_from_model_usage(obj.get("modelUsage"))
    if token_usage.get("token_basis") is None:
        token_usage = _usage_from_top_level(obj.get("usage"))
    if token_usage.get("token_basis") is not None:
        token_usage["usage_raw"] = {"usage": obj.get("usage"), "modelUsage": obj.get("modelUsage")}
    tokens = _legacy_tokens(token_usage)

    turns = obj.get("num_turns")
    turns = int(turns) if isinstance(turns, (int, float)) else None

    ok = obj.get("is_error")
    ok = (not ok) if isinstance(ok, bool) else None

    tail = obj.get("result")
    tail = str(tail)[-2000:] if tail else ""
    return tokens, turns, tail, ok, token_usage



def _parse_json(stdout):
    """Backward-compatible parser returning legacy fields only."""
    tokens, turns, tail, ok, token_usage = _parse_json_with_usage(stdout)
    return tokens, turns, tail, ok

def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model in MODELS:
        spec = MODELS[model]
    elif model in OPEN_MODELS:
        spec = OPEN_MODELS[model]
    else:
        return _unsupported(model)

    key = os.environ.get(spec["env_key"])
    if not key:
        return _setup_needed(spec["env_key"], model)

    iso_home = tempfile.mkdtemp(prefix="claude_home_")
    try:
        env = _clean_env(spec, key, iso_home)
        cmd = [
            _resolve_exe(), "-p",
            "--bare",
            "--output-format", "json",
            "--model", spec["model_id"],
            "--effort", spec["effort"],
            "--dangerously-skip-permissions",
            "--no-session-persistence",
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
                **_empty_token_usage(),
            }

        combined = (proc.stdout or "") + (proc.stderr or "")
        try:
            tokens, turns, tail, ok, token_usage = _parse_json_with_usage(proc.stdout or "")
        except Exception:  # noqa: BLE001 - never let usage parsing break a run
            tokens, turns, tail, ok, token_usage = None, None, "", None, _empty_token_usage()
        if not tail:
            tail = combined[-2000:]

        # completed == harness process exited 0 AND the result was not an error
        # (an API/tool error can still exit 0 with is_error=true).
        completed = proc.returncode == 0 and ok is not False
        if completed:
            error = None
        elif proc.returncode != 0:
            error = f"exit {proc.returncode}"
        else:
            error = "result is_error=true"

        return {
            "completed": completed,
            "error": error,
            "output_tail": tail,
            # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr so the
            # runner can persist a complete local transcript. LOCAL-ONLY:
            # transcripts are never published unscrubbed.
            "full_output": combined,
            "tokens": tokens,
            "turns": turns,
            "cmd": cmd,
            **token_usage,
        }
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
