"""Adapter for the `claude` CLI (Claude Code) running OPEN models.

This adapter deliberately supports ONLY open models served over the vendors'
Anthropic-compatible endpoints. Frontier / subscription models (gpt-5.5 and
Anthropic's own claude-*) are intentionally UNSUPPORTED here so a benchmark run
can never bill the user's Anthropic account (``MODELS`` is empty; unknown models
return the ``_unsupported`` dict).

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
- Token accounting keeps the other adapters' "fresh tokens, cache re-reads
  excluded" basis, BUT the arithmetic differs because Anthropic-style usage
  fields are DISJOINT (unlike codex's inclusive ``input_tokens``):
      tokens = inputTokens + cacheCreationInputTokens + outputTokens
      (cache-READ tokens excluded; ``input_tokens`` already omits cached reads)
  Preferred from cumulative ``modelUsage``; falls back to top-level ``usage``.
  turns  = ``num_turns``.
  Parsing is defensive: shape drift yields tokens=None/turns=None + raw tail.
"""

import json
import os
import shutil
import subprocess
import tempfile

NAME = "claude"
_EXE = "claude"

# This adapter supports open models only; frontier/subscription is deliberately
# excluded so a run can never bill the user's Anthropic account.
MODELS = {}

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
    # Route every request at the vendor host and authenticate with the vendor
    # key ONLY. See module docstring for the billing-safety argument.
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
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


def _setup_needed(env_key, model):
    return {"completed": False,
            "error": f"SETUP-NEEDED: export {env_key} to use {model}",
            "output_tail": "", "tokens": None, "turns": None, "cmd": None}


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
    return (_dec(exc.stdout) + _dec(exc.stderr))[-limit:]


def _tokens_from_model_usage(model_usage):
    """Sum fresh tokens across the per-model cumulative usage map, or None."""
    if not isinstance(model_usage, dict):
        return None
    total = 0
    found = False
    for m in model_usage.values():
        if not isinstance(m, dict):
            continue
        inp = m.get("inputTokens")
        out = m.get("outputTokens")
        if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
            cc = m.get("cacheCreationInputTokens") or 0
            total += int(inp) + int(cc) + int(out)
            found = True
    return total if found else None


def _tokens_from_usage(usage):
    """Fresh tokens from a top-level Anthropic-style usage dict, or None."""
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if not (isinstance(inp, (int, float)) and isinstance(out, (int, float))):
        return None
    cc = usage.get("cache_creation_input_tokens") or 0
    return int(inp) + int(cc) + int(out)


def _parse_json(stdout):
    """Parse claude's `--output-format json` result into (tokens, turns, tail, ok).

    ``ok`` is True/False from ``is_error`` (None if unknown). tokens/turns are
    None when nothing parseable is found; tail is the final ``result`` text.
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
        return None, None, "", None

    tokens = _tokens_from_model_usage(obj.get("modelUsage"))
    if tokens is None:
        tokens = _tokens_from_usage(obj.get("usage"))

    turns = obj.get("num_turns")
    turns = int(turns) if isinstance(turns, (int, float)) else None

    ok = obj.get("is_error")
    ok = (not ok) if isinstance(ok, bool) else None

    tail = obj.get("result")
    tail = str(tail)[-2000:] if tail else ""
    return tokens, turns, tail, ok


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    if model in MODELS:
        # Deliberately unreachable (MODELS is empty): frontier/subscription is
        # unsupported here so a run can never bill the Anthropic account.
        return _unsupported(model)
    if model not in OPEN_MODELS:
        return _unsupported(model)

    spec = OPEN_MODELS[model]
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
            tokens, turns, tail, ok = _parse_json(proc.stdout or "")
        except Exception:  # noqa: BLE001 - never let usage parsing break a run
            tokens, turns, tail, ok = None, None, "", None
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
        }
    finally:
        shutil.rmtree(iso_home, ignore_errors=True)
