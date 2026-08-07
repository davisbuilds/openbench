"""Adapter for the `codex` CLI (OpenAI Codex, ChatGPT-subscription login).

Headless invocation:
    CODEX_HOME=<isolated tmp with auth.json only> codex exec --json \
        --disable apps --disable plugins --disable multi_agent \
        --skip-git-repo-check -C <workdir> -s workspace-write \
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
- Copies only runtime `auth.json` into a fresh `CODEX_HOME`; personal config,
  instructions, skills, plugins, MCPs, rules, memories, and sessions are absent.
- `--json` emits a JSONL event stream. The final `turn.completed` event carries
  `usage={input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}`.
  Token accounting emits TOKEN_PARITY.md split fields from the final aggregate:
    tokens_input_uncached = input_tokens - cached_input_tokens
    tokens_cache_read     = cached_input_tokens
    tokens_cache_write    = 0
    tokens_output         = output_tokens  # already reasoning-inclusive
    tokens_reasoning      = reasoning_output_tokens
    tokens                = tokens_input_uncached + tokens_output
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

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

try:
    from obench.auth_persist import auth_file_lease, auth_lease_proves_path
except ImportError:  # file-path / Docker mount layout
    from auth_persist import auth_file_lease, auth_lease_proves_path

NAME = "codex"
_EXE = "codex"
_MULTI_AGENT_ENV = "OPENBENCH_CODEX_MULTI_AGENT"
_NATIVE_MCP_COMMAND_ENV = "CUB_MCP_COMMAND"
_NATIVE_MARKER_ENVS = (
    "OPENBENCH_NATIVE_MCP_SERVER_COMMAND",
    "OPENBENCH_NATIVE_MCP_LEDGER",
    "OPENBENCH_NATIVE_COLLECTOR_RUN_ID",
    "OPENBENCH_NATIVE_TRIAL_ID",
)
_NATIVE_MODEL = "gpt-5.6-sol"
_NATIVE_ATIF_NAME = "trajectory.json"
_NATIVE_RAW_EVENTS_NAME = "codex-events.jsonl"
_NATIVE_TOOL_POLICY_LEDGER_NAME = "codex-tool-policy.jsonl"
_NATIVE_TOOL_POLICY_HOOK_NAME = "native-tool-policy.py"
_NATIVE_ALLOWED_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
    "error",
}
_NATIVE_NON_TOOL_ITEM_TYPES = {
    "agent_message",
    "reasoning",
    "todo_list",
    "error",
}
_NATIVE_ALLOWED_MCP_SERVER = "computer-use"
_NATIVE_ALLOWED_TOOL_PREFIX = f"mcp__{_NATIVE_ALLOWED_MCP_SERVER}__"
_NATIVE_DISABLED_TOOL_FEATURES = (
    "shell_tool",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "tool_suggest",
    "workspace_dependencies",
    "goals",
)


def _feature_flags(env_override=None):
    """Keep stock runs OFF; only the checked-in candidate explicitly opts ON."""
    flags = ["--disable", "apps", "--disable", "plugins"]
    if env_override and env_override.get(_MULTI_AGENT_ENV) == "enabled":
        flags += ["--enable", "multi_agent"]
    else:
        flags += ["--disable", "multi_agent"]
    return flags


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


def _native_value(name, env_override=None):
    if env_override and name in env_override:
        return env_override[name]
    return os.environ.get(name)


def _native_requested(env_override=None):
    return bool(
        _native_value(_NATIVE_MCP_COMMAND_ENV, env_override)
        or any(_native_value(name, env_override) for name in _NATIVE_MARKER_ENVS)
    )


def _native_launcher(env_override=None):
    value = _native_value(_NATIVE_MCP_COMMAND_ENV, env_override)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{_NATIVE_MCP_COMMAND_ENV} must be an absolute executable file")
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{_NATIVE_MCP_COMMAND_ENV} must be an absolute executable file")
    return value


def _native_error(message):
    return {
        "completed": False,
        "error": f"native-codex-profile: {message}",
        "startup_failure": True,
        "output_tail": "",
        "tokens": None,
        "turns": None,
        "cmd": None,
        **_empty_token_usage(),
    }


def _atomic_write_private_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_private(path, text):
    _atomic_write_private_bytes(path, text.encode("utf-8"))


def _install_native_tool_policy(codex_home, *, launcher):
    ledger_path = Path(launcher).parent / _NATIVE_TOOL_POLICY_LEDGER_NAME
    hook_path = Path(codex_home) / _NATIVE_TOOL_POLICY_HOOK_NAME
    _atomic_write_private(ledger_path, "")
    script = f"""#!{sys.executable}
import hashlib
import json
import os
import sys

LEDGER_PATH = {str(ledger_path)!r}
ALLOWED_PREFIX = {_NATIVE_ALLOWED_TOOL_PREFIX!r}

try:
    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name")
    tool_use_id = payload.get("tool_use_id")
    allowed = (
        isinstance(tool_name, str)
        and tool_name.startswith(ALLOWED_PREFIX)
        and len(tool_name) > len(ALLOWED_PREFIX)
        and isinstance(tool_use_id, str)
        and bool(tool_use_id)
    )
    encoded_input = json.dumps(
        payload.get("tool_input"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    record = {{
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "input_sha256": hashlib.sha256(encoded_input).hexdigest(),
        "decision": "allow" if allowed else "block",
    }}
    fd = os.open(LEDGER_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")
        handle.flush()
        os.fsync(handle.fileno())
    hook_output = {{"hookEventName": "PreToolUse"}}
    if not allowed:
        hook_output.update({{
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "OpenBench native trials permit only computer-use MCP tools"
            ),
        }})
    print(json.dumps({{
        "hookSpecificOutput": hook_output
    }}))
except BaseException:
    print("OpenBench native tool policy hook failed closed", file=sys.stderr)
    raise SystemExit(2)
"""
    _atomic_write_private(hook_path, script)
    hook_path.chmod(0o700)
    hooks = {
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": (
                        f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_path))}"
                    ),
                    "statusMessage": "enforcing native tool policy",
                }],
            }],
        },
    }
    _atomic_write_private(
        Path(codex_home) / "hooks.json",
        json.dumps(hooks, sort_keys=True) + "\n",
    )
    return ledger_path


def _codex_events(stdout):
    events = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Codex JSONL line {line_number} is malformed: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"Codex JSONL line {line_number} is not an object")
        events.append(event)
    if not events:
        raise ValueError("Codex JSONL contains no events")
    return events


def _assert_native_tool_policy(events):
    """Require the native Codex trajectory to use only computer-use MCP tools."""
    for index, event in enumerate(events, start=1):
        event_type = event.get("type")
        if event_type not in _NATIVE_ALLOWED_EVENT_TYPES:
            raise ValueError(
                f"native tool policy rejected unknown event type {event_type!r} "
                f"at event {index}"
            )
        if not event_type.startswith("item."):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            raise ValueError(
                f"native tool policy requires an item object at event {index}"
            )
        item_type = item.get("type")
        if item_type in _NATIVE_NON_TOOL_ITEM_TYPES:
            message = item.get("message")
            if (
                item_type == "error"
                and isinstance(message, str)
                and "dropped" in message.lower()
                and "event" in message.lower()
            ):
                raise ValueError(
                    f"native tool policy rejected incomplete event stream at event {index}"
                )
            continue
        if item_type != "mcp_tool_call":
            raise ValueError(
                f"native tool policy rejected Codex item type {item_type!r} "
                f"at event {index}"
            )
        server = item.get("server")
        if server != _NATIVE_ALLOWED_MCP_SERVER:
            raise ValueError(
                f"native tool policy rejected MCP server {server!r} at event {index}"
            )


def _assert_native_tool_policy_ledger(path, events):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("native tool policy ledger is missing or invalid")
    allowed = []
    blocked = []
    ledger_ids = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"native tool policy ledger line {line_number} is malformed: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"native tool policy ledger line {line_number} is not an object"
            )
        tool_name = record.get("tool_name")
        tool_use_id = record.get("tool_use_id")
        input_sha256 = record.get("input_sha256")
        decision = record.get("decision")
        if (
            set(record) != {
                "tool_name",
                "tool_use_id",
                "input_sha256",
                "decision",
            }
            or
            not isinstance(tool_name, str)
            or not isinstance(tool_use_id, str)
            or not tool_use_id
            or not isinstance(input_sha256, str)
            or len(input_sha256) != 64
            or any(char not in "0123456789abcdef" for char in input_sha256)
            or decision not in {"allow", "block"}
        ):
            raise ValueError(
                f"native tool policy ledger line {line_number} has invalid fields"
            )
        if tool_use_id in ledger_ids:
            raise ValueError(
                f"native tool policy ledger repeats tool_use_id {tool_use_id!r}"
            )
        ledger_ids.add(tool_use_id)
        if decision == "allow":
            if not tool_name.startswith(_NATIVE_ALLOWED_TOOL_PREFIX):
                raise ValueError(
                    f"native tool policy ledger allowed forbidden tool {tool_name!r}"
                )
            allowed.append((tool_name, input_sha256))
        else:
            blocked.append(tool_name)
    if blocked:
        raise ValueError(
            f"native tool policy blocked forbidden tool {blocked[0]!r}"
        )

    trajectory_tools = {}
    trajectory_lifecycle = {}
    for event in events:
        item = event.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "mcp_tool_call"
            or not isinstance(item.get("id"), str)
        ):
            continue
        arguments = item.get("arguments")
        encoded_arguments = json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        identity = (
            f"mcp__{item.get('server')}__{item.get('tool')}",
            hashlib.sha256(encoded_arguments).hexdigest(),
        )
        existing = trajectory_tools.setdefault(item["id"], identity)
        if existing != identity:
            raise ValueError(
                f"Codex MCP trajectory item {item['id']!r} changed identity"
            )
        lifecycle = trajectory_lifecycle.setdefault(
            item["id"],
            {"item.started": 0, "item.completed": 0},
        )
        event_type = event.get("type")
        if event_type not in lifecycle:
            raise ValueError(
                f"Codex MCP trajectory item {item['id']!r} has unexpected "
                f"lifecycle event {event_type!r}"
            )
        lifecycle[event_type] += 1
    for item_id, lifecycle in trajectory_lifecycle.items():
        if lifecycle != {"item.started": 1, "item.completed": 1}:
            raise ValueError(
                f"Codex MCP trajectory item {item_id!r} has incomplete lifecycle"
            )
    if Counter(allowed) != Counter(trajectory_tools.values()):
        raise ValueError(
            "native tool policy ledger does not match Codex MCP trajectory"
        )


def _write_and_verify_native_events(stdout, *, launcher, tool_policy_ledger):
    raw_path = Path(launcher).parent / _NATIVE_RAW_EVENTS_NAME
    if isinstance(stdout, bytes):
        raw_bytes = stdout
    else:
        raw_bytes = stdout.encode("utf-8")
    _atomic_write_private_bytes(raw_path, raw_bytes)
    decoded = raw_bytes.decode("utf-8")
    events = _codex_events(decoded)
    _assert_native_tool_policy(events)
    _assert_native_tool_policy_ledger(tool_policy_ledger, events)
    return events


def _write_native_evidence(
    stdout,
    *,
    launcher,
    tool_policy_ledger,
    workdir,
    model,
):
    from obench.atif import assert_valid_trajectory, to_dict
    from obench.tools.atif_convert import convert_codex_events

    events = _write_and_verify_native_events(
        stdout,
        launcher=launcher,
        tool_policy_ledger=tool_policy_ledger,
    )
    trajectory = convert_codex_events(
        events,
        model=model,
        source_path=_NATIVE_RAW_EVENTS_NAME,
    )
    trajectory = to_dict(trajectory)
    trajectory["extra"]["tool_policy"] = {
        "mode": "native_mcp_only",
        "allowed_mcp_servers": [_NATIVE_ALLOWED_MCP_SERVER],
        "verified": True,
    }
    assert_valid_trajectory(trajectory)
    rendered = json.dumps(trajectory, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_private(Path(workdir) / _NATIVE_ATIF_NAME, rendered)

# canonical model name -> codex `-m` model string
MODELS = {
    "gpt-5.5-medium": "gpt-5.5",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna": "gpt-5.6-luna",
}

# canonical model name -> reasoning effort passed via `-c model_reasoning_effort`
_EFFORT = {
    "gpt-5.5-medium": "medium",
    "gpt-5.6-sol": "medium",
    "gpt-5.6-terra": "medium",
    "gpt-5.6-luna": "medium",
}

# canonical model name -> service tier override. GPT-5.6 Sol must stay on the
# normal/non-fast lane even if the operator's Codex config defaults to priority.
_SERVICE_TIER = {
    "gpt-5.6-sol": "default",
    "gpt-5.6-terra": "default",
    "gpt-5.6-luna": "default",
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
    # Thinking parity for the opus frontier lane: codex requests
    # `model_reasoning_effort="medium"`; the LiteLLM bridge preserves that as
    # Anthropic medium reasoning while injecting ANTHROPIC_API_KEY upstream.
    "claude-opus-4-8":   {"provider": "anthropic", "model_id": "claude-opus-4-8",   "base_url": "https://api.anthropic.com",     "env_key": "ANTHROPIC_API_KEY", "display": "Anthropic Claude", "effort": "medium"},
    "glm-5.2":           {"provider": "zai",      "model_id": "glm-5.2",           "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "effort": "medium"},
    "glm-4.7-flash":     {"provider": "zai",      "model_id": "glm-4.7-flash",     "base_url": "https://api.z.ai/api/paas/v4", "env_key": "ZAI_API_KEY",      "display": "Z.ai GLM",      "effort": "medium"},
    "deepseek-v4-flash": {"provider": "deepseek", "model_id": "deepseek-v4-flash", "base_url": "https://api.deepseek.com",     "env_key": "DEEPSEEK_API_KEY", "display": "DeepSeek",      "effort": "medium"},
    "kimi-k2.7-code":    {"provider": "moonshot", "model_id": "kimi-k2.7-code",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi", "effort": "medium"},
    "kimi-k3":    {"provider": "moonshot", "model_id": "kimi-k3",    "base_url": "https://api.moonshot.ai/v1",   "env_key": "MOONSHOT_API_KEY", "display": "Moonshot Kimi K3", "effort": "medium"},
}

# Host-side bridge (LiteLLM proxy). Port must match bench/openmodel_bridge.sh
# (both default to 4141; override in lockstep via BENCH_BRIDGE_PORT).
_BRIDGE_DEFAULT_PORT = 4141
_KEYS_ENV = os.path.expanduser("~/.openbench/keys.env")


def _proxy_cell_url(*parts):
    base = os.environ.get("OPENBENCH_PROXY_BASE_URL")
    token = os.environ.get("OPENBENCH_PROXY_CELL_TOKEN")
    if not os.environ.get("OPENBENCH_PROXY") or not base or not token:
        return None
    path = "/".join(str(p).strip("/") for p in ("cell", token, *parts) if str(p).strip("/"))
    return base.rstrip("/") + "/" + path


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
    """codex ``base_url`` for the bridge; codex appends ``/responses`` to it.

    When the counting proxy is active, route through its ``bridge`` prefix
    (proxy -> LiteLLM -> vendor) so open-model cells get proxy-metered usage.
    """
    proxied = _proxy_cell_url("bridge", "v1")
    if proxied:
        return proxied
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
            "output_tail": "", "tokens": None, "turns": None, "cmd": None,
            **_empty_token_usage()}


def _keys_env_has(env_key):
    try:
        with open(_KEYS_ENV, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                try:
                    parts = shlex.split(line, comments=True, posix=True)
                    first = parts[1] if parts and parts[0] == "export" and len(parts) > 1 else parts[0]
                    key, val = first.split("=", 1)
                except (ValueError, IndexError):
                    key, val = line.split("=", 1)[0].strip(), line.split("=", 1)[1].strip()
                if key == env_key and val.strip():
                    return True
    except OSError:
        return False
    return False


def _host_has_key(env_key):
    return bool(os.environ.get(env_key) or _keys_env_has(env_key))


def _codex_env_for_bridge(env_key):
    # The bridge injects the real upstream key from its host process. Give codex
    # only a non-secret placeholder so its shell-capable agent cannot read API
    # credentials from the environment.
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env[env_key] = "openbench-bridge-placeholder"
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
    text = _dec(exc.stdout) + _dec(exc.stderr)
    return text if limit is None else text[-limit:]


def _parse_json_with_usage(stdout):
    """Parse codex's JSONL event stream into (tokens, turns, tail, usage).

    Codex's final aggregate ``input_tokens`` is cache-inclusive and
    ``output_tokens`` is reasoning-inclusive. Reasoning is recorded separately
    as a subset and must not be added to the legacy scalar.
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

    token_usage = _empty_token_usage()
    usage_raw = []
    last_usage = None
    turns = 0
    transcript = []
    for ev in events:
        etype = ev.get("type")
        if etype == "turn.completed":
            turns += 1
            usage = ev.get("usage") or {}
            if isinstance(usage, dict):
                usage_raw.append(usage)
                last_usage = usage
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

    if last_usage is not None:
        inp = _num(last_usage.get("input_tokens"))
        cached = _num(last_usage.get("cached_input_tokens"))
        cache_write = _num(
            last_usage.get("cache_write_tokens")
            or last_usage.get("cache_creation_input_tokens")
            or last_usage.get("cache_creation_tokens")
            or 0
        )
        out = _num(last_usage.get("output_tokens"))
        reasoning = _num(last_usage.get("reasoning_output_tokens"))
        invariant_ok = None not in (inp, cached, cache_write, out, reasoning)
        if invariant_ok and (cached + cache_write > inp or reasoning > out):
            invariant_ok = False
        if invariant_ok:
            token_usage.update({
                "tokens_input_uncached": inp - cached - cache_write,
                "tokens_cache_read": cached,
                "tokens_cache_write": cache_write,
                "tokens_output": out,
                "tokens_reasoning": reasoning,
            })
        token_usage["usage_raw"] = last_usage
        token_usage["token_basis"] = "vendor_split" if invariant_ok else "estimated"

    tail = "\n".join(transcript)[-2000:]
    return _legacy_tokens(token_usage), (turns or None), tail, token_usage



def _parse_json(stdout):
    """Backward-compatible parser returning legacy fields only."""
    tokens, turns, tail, token_usage = _parse_json_with_usage(stdout)
    return tokens, turns, tail

def run(
    instruction: str,
    workdir: str,
    model: str,
    timeout_s: int,
    env_override=None,
    auth_lease_proofs=(),
) -> dict:
    native = _native_requested(env_override)
    native_launcher = None
    if native:
        if model != _NATIVE_MODEL:
            return _native_error(f"model must be {_NATIVE_MODEL!r}, got {model!r}")
        try:
            native_launcher = _native_launcher(env_override)
        except ValueError as exc:
            return _native_error(str(exc))

    if native:
        # Disable known builtin tool families and block every remaining model
        # tool through a deny-by-default PreToolUse hook. Read-only is a second
        # barrier for the model-catalog-driven patch tool.
        sandbox = ["-s", "read-only"]
    elif os.environ.get("BENCH_IN_CONTAINER"):
        # codex's own sandbox (bwrap) needs user namespaces and cannot nest
        # inside the bench container; the disposable container IS the external
        # sandbox, which is the documented intent of this flag.
        sandbox = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        sandbox = ["-s", "workspace-write"]
    base = [
        "codex", "exec",
        "--json",
    ] + _feature_flags(env_override) + [
        "--skip-git-repo-check",
        "-C", workdir,
    ] + sandbox
    if native_launcher:
        for feature in _NATIVE_DISABLED_TOOL_FEATURES:
            base += ["--disable", feature]
        base += [
            "--enable", "hooks",
            "--dangerously-bypass-hook-trust",
            "-c", f"mcp_servers.computer-use.command={json.dumps(native_launcher)}",
            "-c", "mcp_servers.computer-use.args=[]",
        ]
    if model in MODELS:
        cmd = base + [
            "-m", MODELS[model],
            "-c", f'model_reasoning_effort="{_EFFORT[model]}"',
        ]
        if model in _SERVICE_TIER:
            cmd += ["-c", f'service_tier="{_SERVICE_TIER[model]}"']
        proxy_url = _proxy_cell_url("codex", "backend-api", "codex")
        if proxy_url:
            cmd += ["-c", f'openai_base_url="{proxy_url}"']
        cmd += [instruction]
    elif model in OPEN_MODELS:
        spec = OPEN_MODELS[model]
        if not _host_has_key(spec["env_key"]):
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

    child_env = _codex_env_for_bridge(spec["env_key"]) if model in OPEN_MODELS else os.environ.copy()
    if env_override:
        child_env.update(env_override)
        # This is an adapter control, not child-process configuration.
        child_env.pop(_MULTI_AGENT_ENV, None)

    # Stock runs get a fresh CODEX_HOME containing authentication only.  In
    # particular, never copy config.toml, AGENTS.md, skills, MCP definitions,
    # rules, memories, sessions, or plugins from the machine owner. Ablation
    # adapters supply their own already-composed CODEX_HOME via env_override.
    isolated_home = None
    native_tool_policy_ledger = None
    auth_src = None
    auth_copy = None
    auth_lease = None
    provided_codex_home = None if native else (
        env_override.get("CODEX_HOME") if env_override else None
    )
    if provided_codex_home:
        auth_src = os.path.join(
            os.path.expanduser(provided_codex_home), "auth.json"
        )
        if (os.path.isfile(auth_src)
                and not auth_lease_proves_path(
                    auth_lease_proofs, auth_src
                )):
            auth_lease = auth_file_lease(auth_src).__enter__()
    else:
        isolated_home = tempfile.mkdtemp(prefix="codex_home_")
        auth_root = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
        auth_src = os.path.join(auth_root, "auth.json")
        if os.path.isfile(auth_src):
            try:
                auth_lease = auth_file_lease(auth_src).__enter__()
                auth_copy = os.path.join(isolated_home, "auth.json")
                auth_lease.stage(auth_copy)
            except BaseException:
                if auth_lease is not None:
                    auth_lease.__exit__(None, None, None)
                shutil.rmtree(isolated_home, ignore_errors=True)
                raise
        child_env["CODEX_HOME"] = isolated_home

    try:
        try:
            if native_launcher:
                native_tool_policy_ledger = _install_native_tool_policy(
                    child_env["CODEX_HOME"],
                    launcher=native_launcher,
                )
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=not native,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                env=child_env,
            )
        except subprocess.TimeoutExpired as e:
            full_output = _err_tail(e, limit=None)
            native_timeout_evidence_error = None
            if native_launcher:
                try:
                    _write_and_verify_native_events(
                        e.stdout or b"",
                        launcher=native_launcher,
                        tool_policy_ledger=native_tool_policy_ledger,
                    )
                except (OSError, TypeError, ValueError) as exc:
                    native_timeout_evidence_error = str(exc)
            error = f"timeout after {timeout_s}s"
            if native_timeout_evidence_error is not None:
                error += (
                    "; native evidence verification failed: "
                    f"{native_timeout_evidence_error}"
                )
            return {
                "completed": False,
                "error": error,
                "output_tail": full_output[-2000:],
                "full_output": full_output,
                "tokens": None,
                "turns": None,
                "cmd": cmd,
                **_empty_token_usage(),
            }
    finally:
        try:
            if auth_copy and auth_lease:
                auth_lease.try_persist(auth_copy)
        finally:
            if auth_lease:
                auth_lease.__exit__(None, None, None)
            if isolated_home:
                shutil.rmtree(isolated_home, ignore_errors=True)

    raw_stdout = proc.stdout or (b"" if native else "")
    raw_stderr = proc.stderr or (b"" if native else "")
    stdout_text = (
        raw_stdout.decode("utf-8", "replace")
        if isinstance(raw_stdout, bytes)
        else raw_stdout
    )
    stderr_text = (
        raw_stderr.decode("utf-8", "replace")
        if isinstance(raw_stderr, bytes)
        else raw_stderr
    )
    combined = stdout_text + stderr_text
    try:
        tokens, turns, tail, token_usage = _parse_json_with_usage(stdout_text)
    except Exception:  # noqa: BLE001 - never let usage parsing break a run
        tokens, turns, tail, token_usage = None, None, "", _empty_token_usage()
    if not tail:
        tail = combined[-2000:]

    native_evidence_error = None
    if native_launcher:
        try:
            _write_native_evidence(
                raw_stdout,
                launcher=native_launcher,
                tool_policy_ledger=native_tool_policy_ledger,
                workdir=workdir,
                model=model,
            )
        except (OSError, TypeError, ValueError) as exc:
            native_evidence_error = str(exc)

    if model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna") and token_usage.get("token_basis") == "vendor_split":
        raw = token_usage.get("usage_raw") or {}
        if not any(k in raw for k in ("cache_write_tokens", "cache_creation_input_tokens", "cache_creation_tokens")):
            # GPT-5.6 may expose billable cache writes on newer Codex event
            # schemas. If this CLI omits the field, keep the legacy fresh-ish
            # scalar usable for the smoke contract but do not assert complete
            # split parity: cache writes are unknown and the uncached lane may
            # include writes depending on Codex's aggregate input semantics.
            token_usage["tokens_cache_write"] = None
            token_usage["token_basis"] = "estimated"

    return {
        "completed": proc.returncode == 0 and native_evidence_error is None,
        "error": (
            f"native ATIF conversion failed: {native_evidence_error}"
            if native_evidence_error is not None
            else None if proc.returncode == 0 else f"exit {proc.returncode}"
        ),
        "output_tail": tail,
        # Optional (ADAPTER_SPEC v1): full untruncated stdout+stderr so the
        # runner can persist a complete local transcript. Cheap here (already
        # concatenated). LOCAL-ONLY: transcripts are never published unscrubbed.
        "full_output": combined,
        "tokens": tokens,
        "turns": turns,
        "cmd": cmd,
        **token_usage,
    }
