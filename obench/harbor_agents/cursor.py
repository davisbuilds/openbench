"""Harbor Cursor subscription profile with strict source-derived ATIF."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shlex
from typing import Any

from obench.atif import SCHEMA_VERSION, assert_valid_trajectory, dump_trajectory
from obench.harbor_agents._subscription import (
    CURSOR_AUTH_ARCHIVE_ENV,
    resolve_subscription_archive,
    upload_subscription_archive,
)
from obench.harbor_oauth import (
    HarborOAuthSetupError,
    HarborOAuthUnsupportedError,
)


_REMOTE_HOME = PurePosixPath("/tmp/openbench-cursor-home")
_REMOTE_ARCHIVE = PurePosixPath("/tmp/openbench-cursor-auth.tar.gz")
_OUTPUT_FILENAME = "cursor.jsonl"
_CURSOR_MODELS = {
    "gpt-5.5-medium": "gpt-5.5-medium",
    "gpt-5.6-sol": "gpt-5.6-sol-medium",
    "gpt-5.6-terra": "gpt-5.6-terra-medium",
    "gpt-5.6-luna": "gpt-5.6-luna-medium",
}


def convert_cursor_stream(
    source: str | Path,
    *,
    version: str,
    model_name: str,
) -> dict[str, Any]:
    """Convert documented Cursor stream-json events without inventing steps."""

    source_path = Path(source)
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HarborOAuthUnsupportedError(
            "Cursor did not produce a readable stream-json log"
        ) from exc

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarborOAuthUnsupportedError(
                f"Cursor stream-json line {line_number} is not JSON"
            ) from exc
        if not isinstance(event, dict):
            raise HarborOAuthUnsupportedError(
                f"Cursor stream-json line {line_number} is not an object"
            )
        events.append(event)

    terminal = next(
        (event for event in reversed(events) if event.get("type") == "result"),
        None,
    )
    if (
        terminal is None
        or terminal.get("subtype") != "success"
        or terminal.get("is_error") not in (None, False)
    ):
        raise HarborOAuthUnsupportedError(
            "Cursor stream-json lacks a successful terminal result event"
        )

    steps: list[dict[str, Any]] = []
    pending_tools: dict[str, dict[str, Any]] = {}
    saw_assistant = False
    session_id = terminal.get("session_id")
    for event in events:
        event_type = event.get("type")
        if event_type in {"user", "assistant"}:
            message = _message_content(event.get("message"))
            if message is None:
                continue
            if event_type == "assistant":
                saw_assistant = True
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "source": "agent" if event_type == "assistant" else "user",
                    "message": message,
                    **(
                        {"model_name": model_name}
                        if event_type == "assistant"
                        else {}
                    ),
                }
            )
        elif event_type == "tool_call":
            call_id = event.get("call_id")
            tool = event.get("tool_call")
            if not isinstance(call_id, str) or not call_id or not isinstance(tool, dict):
                continue
            pending_tools[call_id] = _merge_tool_event(
                pending_tools.get(call_id),
                tool,
            )
            if event.get("subtype") == "completed":
                steps.append(
                    _tool_step(
                        len(steps) + 1,
                        call_id,
                        pending_tools.pop(call_id),
                        model_name,
                    )
                )

    for call_id, tool in pending_tools.items():
        steps.append(_tool_step(len(steps) + 1, call_id, tool, model_name))

    if not saw_assistant:
        result = terminal.get("result")
        if isinstance(result, str) and result:
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "source": "agent",
                    "message": result,
                    "model_name": model_name,
                }
            )
    if not any(step["source"] == "agent" for step in steps):
        raise HarborOAuthUnsupportedError(
            "Cursor source log contains no attributable agent action"
        )

    usage = terminal.get("usage")
    if isinstance(usage, dict):
        agent_steps = [step for step in steps if step["source"] == "agent"]
        metrics = _cursor_metrics(usage)
        if metrics:
            agent_steps[-1]["metrics"] = metrics

    final_metrics: dict[str, Any] = {"total_steps": len(steps)}
    if isinstance(usage, dict):
        metric_names = {
            "inputTokens": "total_prompt_tokens",
            "outputTokens": "total_completion_tokens",
            "cacheReadTokens": "total_cached_tokens",
        }
        for source_name, target_name in metric_names.items():
            value = usage.get(source_name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                final_metrics[target_name] = value

    trajectory = {
        "schema_version": SCHEMA_VERSION,
        "agent": {
            "name": "cursor",
            "version": version,
            "model_name": model_name,
        },
        "steps": steps,
        "final_metrics": final_metrics,
        "extra": {
            "source_format": "cursor-stream-json",
            "source_transcript": str(source_path),
        },
    }
    if isinstance(session_id, str) and session_id:
        trajectory["session_id"] = session_id
    assert_valid_trajectory(trajectory)
    return trajectory


def _message_content(message: Any) -> str | list[dict[str, Any]] | None:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [
        {"type": "text", "text": part["text"]}
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    return parts or None


def _merge_tool_event(
    prior: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(prior or {})
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _tool_step(
    step_id: int,
    call_id: str,
    tool: dict[str, Any],
    model_name: str,
) -> dict[str, Any]:
    tool_name, payload = next(
        (
            (key.removesuffix("ToolCall"), value)
            for key, value in tool.items()
            if key.endswith("ToolCall") and isinstance(value, dict)
        ),
        ("unknown", {}),
    )
    arguments = payload.get("args")
    if not isinstance(arguments, dict):
        arguments = {}
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": "agent",
        "message": "",
        "model_name": model_name,
        "tool_calls": [
            {
                "tool_call_id": call_id,
                "function_name": tool_name,
                "arguments": arguments,
            }
        ],
    }
    result = payload.get("result")
    if result is not None:
        step["observation"] = {
            "results": [
                {
                    "source_call_id": call_id,
                    "content": json.dumps(
                        result,
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                }
            ]
        }
    return step


def _cursor_metrics(usage: dict[str, Any]) -> dict[str, int]:
    result = {}
    for source_name, target_name in (
        ("inputTokens", "prompt_tokens"),
        ("outputTokens", "completion_tokens"),
        ("cacheReadTokens", "cached_tokens"),
    ):
        value = usage.get(source_name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[target_name] = value
    return result


def _load_harbor_base():
    try:
        from harbor.agents.installed.base import BaseInstalledAgent
        from harbor.models.trial.paths import EnvironmentPaths
    except (ImportError, ModuleNotFoundError) as exc:
        raise HarborOAuthSetupError(
            "Harbor Cursor support requires Harbor 0.20.0"
        ) from exc
    return BaseInstalledAgent, EnvironmentPaths


def _build_agent_class(base, environment_paths):
    class OpenBenchCursorSubscription(base):
        SUPPORTS_ATIF = True
        SUPPORTS_RESUME = False

        @staticmethod
        def name():
            return "cursor"

        def get_version_command(self):
            return "cursor-agent --version"

        async def install(self, environment):
            version = shlex.quote(self._version or "")
            await self.ensure_system_dependencies(
                environment, ("curl", "tar", "ca_certificates")
            )
            await self.exec_as_root(
                environment,
                command=(
                    'case "$(uname -m)" in '
                    'x86_64|amd64) arch=x64 ;; '
                    'arm64|aarch64) arch=arm64 ;; '
                    '*) echo "unsupported cursor architecture" >&2; exit 1 ;; '
                    "esac; "
                    "rm -rf /installed-agent/cursor && "
                    "mkdir -p /installed-agent/cursor && "
                    f"curl -fsSL https://downloads.cursor.com/lab/{version}/"
                    "linux/$arch/agent-cli-package.tar.gz | "
                    "tar --strip-components=1 -xzf - "
                    "-C /installed-agent/cursor && "
                    "ln -sf /installed-agent/cursor/cursor-agent "
                    "/usr/local/bin/cursor-agent && "
                    f'test "$(cursor-agent --version)" = {version}'
                ),
            )

        async def run(self, instruction, environment, context):
            del context
            archive = resolve_subscription_archive(
                self, CURSOR_AUTH_ARCHIVE_ENV
            )
            model = _CURSOR_MODELS.get(self.model_name or "")
            if model is None:
                raise HarborOAuthUnsupportedError(
                    f"unsupported Cursor model: {self.model_name!r}"
                )
            remote_home = _REMOTE_HOME.as_posix()
            remote_archive = _REMOTE_ARCHIVE.as_posix()
            await self.exec_as_agent(
                environment,
                command=(
                    f"rm -rf {shlex.quote(remote_home)} "
                    f"{shlex.quote(remote_archive)} && "
                    f"mkdir -p {shlex.quote(remote_home)}"
                ),
            )
            await upload_subscription_archive(
                self,
                environment,
                archive=archive,
                remote_archive=remote_archive,
            )
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"tar -xzf {shlex.quote(remote_archive)} "
                        f"-C {shlex.quote(remote_home)} && "
                        f"find {shlex.quote(remote_home)} -type d "
                        "-exec chmod 700 {} + && "
                        f"find {shlex.quote(remote_home)} -type f "
                        "-exec chmod 600 {} + && "
                        f"export HOME={shlex.quote(remote_home)} "
                        f"XDG_CONFIG_HOME={shlex.quote(remote_home + '/.config')} "
                        f"XDG_DATA_HOME={shlex.quote(remote_home + '/.local/share')} "
                        f"XDG_STATE_HOME={shlex.quote(remote_home + '/.local/state')} "
                        f"XDG_CACHE_HOME={shlex.quote(remote_home + '/.cache')}; "
                        "unset CURSOR_API_KEY CURSOR_API_ENDPOINT; "
                        "cursor-agent -p --force --trust "
                        f"--model {shlex.quote(model)} "
                        "--output-format stream-json "
                        f"--workspace {shlex.quote(environment_paths.app_dir.as_posix())} "
                        f"{shlex.quote(self.render_instruction(instruction))} "
                        f"> /logs/agent/{_OUTPUT_FILENAME} "
                        "2> /logs/agent/cursor.stderr </dev/null"
                    ),
                    cwd=environment_paths.app_dir.as_posix(),
                )
            finally:
                try:
                    await self.exec_as_agent(
                        environment,
                        command=(
                            f"rm -rf {shlex.quote(remote_home)} "
                            f"{shlex.quote(remote_archive)}"
                        ),
                    )
                except BaseException:
                    pass

        def populate_context_post_run(self, context):
            del context
            source = self.logs_dir / _OUTPUT_FILENAME
            trajectory = convert_cursor_stream(
                source,
                version=self.version() or "unknown",
                model_name=self.model_name or "unknown",
            )
            dump_trajectory(trajectory, self.logs_dir / "trajectory.json")

    OpenBenchCursorSubscription.__name__ = "OpenBenchCursorSubscription"
    OpenBenchCursorSubscription.__qualname__ = "OpenBenchCursorSubscription"
    OpenBenchCursorSubscription.__module__ = __name__
    return OpenBenchCursorSubscription


def load_agent_class():
    base, environment_paths = _load_harbor_base()
    return _build_agent_class(base, environment_paths)


def __getattr__(name):
    if name != "OpenBenchCursorSubscription":
        raise AttributeError(name)
    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
