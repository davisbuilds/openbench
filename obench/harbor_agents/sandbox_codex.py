"""Codex CLI in the repair sandbox; provider credentials stay in the broker.

Harbor remains an optional dependency. The pinned CLI performs its ordinary
run/tool loop; only its HTTP transport is routed through the narrow gateway.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
import shlex
import shutil
import stat

CLI_VERSION = "0.154.0"
MODELS = {"gpt-5.6-terra": "xhigh", "gpt-5.6-luna": "max"}


def _restore_explicit_zero_totals(metrics, session_dir):
    # Harbor 0.20.0 uses `value or None` for final token counts. Restore only
    # zeros actually reported in the same last totals record Harbor selects;
    # absent usage must remain absent so strict import can detect it.
    session_files = list(session_dir.glob("*.jsonl"))
    if not session_files:
        return
    totals = {}
    with session_files[0].open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                totals = info["total_token_usage"]
    for source, target in (("input_tokens", "total_prompt_tokens"),
                           ("output_tokens", "total_completion_tokens"),
                           ("cached_input_tokens", "total_cached_tokens")):
        value = totals.get(source)
        if type(value) is int and value == 0 and getattr(metrics, target) is None:
            setattr(metrics, target, 0)


def codex_config(port: int = 8765) -> dict:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid loopback relay port")
    return {
        "model_provider": "openbench_isolated",
        "web_search": "disabled",
        "service_tier": "default",
        "model_providers": {"openbench_isolated": {
            "name": "OpenBench isolated model gateway",
            "base_url": f"http://127.0.0.1:{port}",
            "wire_api": "responses",
            "env_key": "OPENAI_API_KEY",
            "supports_websockets": False,
        }},
        "features": {"apps": False, "plugins": False, "multi_agent": False,
                     "enable_request_compression": False},
    }


def staged_auth_paths(auth: str, returned: str) -> tuple[Path, Path]:
    source, destination = Path(auth), Path(returned)
    if (not source.is_absolute() or not destination.is_absolute()
            or source == destination or source.parent != destination.parent
            or source.parent.is_symlink()
            or source.is_symlink() or not source.is_file()
            or source.stat().st_nlink != 1 or destination.is_symlink()
            or (destination.exists() and (not destination.is_file() or destination.stat().st_nlink != 1))):
        raise RuntimeError("unsafe staged auth paths")
    if (stat.S_IMODE(source.parent.stat().st_mode) != 0o700
            or stat.S_IMODE(source.stat().st_mode) != 0o600):
        raise RuntimeError("auth staging directory and file must be private")
    return source, destination


def _build_agent_class(codex):
    class SandboxCodex(codex):
        SUPPORTS_RESUME = False

        def __init__(self, *args, **kwargs):
            if kwargs.get("version") != CLI_VERSION:
                raise ValueError(f"repair sandbox requires Codex {CLI_VERSION}")
            if kwargs.get("config") is not None or kwargs.get("skills_dir") is not None:
                raise ValueError("repair sandbox does not accept ambient config or skills")
            kwargs["config"] = codex_config()
            kwargs["web_search"] = "disabled"
            super().__init__(*args, **kwargs)
            if self.model_name not in MODELS or self._resolved_flags.get("reasoning_effort") != MODELS[self.model_name]:
                raise ValueError("repair sandbox model/effort pair is not admitted")
            if getattr(self, "mcp_servers", None) or getattr(self, "_mcp_servers", None):
                raise ValueError("repair sandbox forbids external MCP servers")

        def _get_env(self, key):
            # Harbor's ordinary Codex run path receives only a dummy transport
            # credential. Actual auth is read by the trusted start_gateway call.
            if key == "OPENAI_API_KEY":
                return "openbench-sandbox-placeholder"
            if key == "OPENAI_BASE_URL":
                return "http://127.0.0.1:8765"
            if key in {"CODEX_AUTH_JSON_PATH", "CODEX_FORCE_AUTH_JSON"}:
                return None
            return super()._get_env(key)

        def _resolve_auth_json_path(self):
            return None

        def _convert_events_to_trajectory(self, session_dir):
            self._last_usage_snapshot = None
            try:
                trajectory = super()._convert_events_to_trajectory(session_dir)
                metrics = getattr(trajectory, "final_metrics", None)
                if metrics is not None:
                    _restore_explicit_zero_totals(metrics, session_dir)
                return trajectory
            finally:
                self._last_usage_snapshot = None

        def _metrics_from_token_count_payload(self, payload):
            # Codex can repeat a cumulative usage report after partial model
            # output (e.g. a retry). Harbor 0.20.0 otherwise charges last_usage
            # again while keeping the unchanged cumulative final metrics.
            info = payload.get("info")
            if isinstance(info, dict):
                total, last = info.get("total_token_usage"), info.get("last_token_usage")
                if (isinstance(total, dict) and isinstance(last, dict)
                        and all("input_tokens" in item and "output_tokens" in item
                                and all(type(value) is int and value >= 0 for value in item.values())
                                for item in (total, last))):
                    snapshot = (dict(total), dict(last))
                    if snapshot == getattr(self, "_last_usage_snapshot", None):
                        return None
                    self._last_usage_snapshot = snapshot
            return super()._metrics_from_token_count_payload(payload)

        async def _upload_config_text(self, environment, *, content, remote_path, filename):
            # Write as the confined agent; no host path or privileged chown.
            encoded = base64.b64encode(content.encode()).decode()
            program = ("import base64,pathlib; p=pathlib.Path(" + repr(remote_path) + "); "
                       "p.parent.mkdir(parents=True,exist_ok=True); "
                       "p.write_bytes(base64.b64decode(" + repr(encoded) + ")); p.chmod(0o600)")
            await self.exec_as_agent(environment, "python3 -c " + shlex.quote(program))

        async def setup(self, environment):
            from obench.harbor_sandbox import RepairSandbox
            if not isinstance(environment, RepairSandbox):
                raise RuntimeError("SandboxCodex requires the enforced RepairSandbox environment")
            result = await self.exec_as_agent(environment, "codex --version")
            actual = self.parse_version(result.stdout)
            if actual != CLI_VERSION:
                raise RuntimeError("installed Codex version does not match the pinned sandbox treatment")
            self._version = actual
            self._sandbox_version_verified = True

        async def run(self, instruction, environment, context):
            if not getattr(self, "_sandbox_version_verified", False):
                raise RuntimeError("sandbox setup/version verification did not run")
            auth = super()._get_env("CODEX_AUTH_JSON_PATH")
            returned = super()._get_env("OPENBENCH_CODEX_AUTH_RETURN_PATH")
            if not auth or not returned:
                raise RuntimeError("trusted staged auth paths are required")
            source, destination = staged_auth_paths(auth, returned)
            try:
                await environment.start_gateway(self.model_name, MODELS[self.model_name], str(source))
                await super().run(instruction, environment, context)
            finally:
                # This kills background tool processes before Harbor collects
                # artifacts or dispatches the trusted verifier.
                await environment.seal()
                # This lane deliberately does not refresh or mutate credentials.
                # Preserve the existing trusted suite credential lifecycle.
                fd, temporary = tempfile.mkstemp(prefix="auth-return-", dir=source.parent)
                try:
                    with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
                        shutil.copyfileobj(original, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                finally:
                    Path(temporary).unlink(missing_ok=True)

    SandboxCodex.__name__ = "SandboxCodex"
    SandboxCodex.__qualname__ = "SandboxCodex"
    SandboxCodex.__module__ = __name__
    return SandboxCodex


def load_agent_class():
    from harbor.agents.installed.codex import Codex
    return _build_agent_class(Codex)


def __getattr__(name):
    if name != "SandboxCodex":
        raise AttributeError(name)
    cls = load_agent_class()
    globals()[name] = cls
    return cls
