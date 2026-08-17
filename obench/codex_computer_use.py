"""Convert official Codex Computer Use events into native MCP evidence."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .mcp_stdio_collector import COMPUTER_USE_TOOLS


_SKY_CALL = re.compile(r"\bsky\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


class CodexComputerUseEvidenceError(ValueError):
    """Official Codex Computer Use events are incomplete or inconsistent."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CodexComputerUseEvidenceError(
            f"cannot read official Codex events: {exc}"
        ) from exc
    events = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise CodexComputerUseEvidenceError(
                f"official Codex events contain a blank line at {line_number}"
            )
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexComputerUseEvidenceError(
                f"official Codex event {line_number} is malformed: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise CodexComputerUseEvidenceError(
                f"official Codex event {line_number} is not an object"
            )
        events.append(event)
    if not events:
        raise CodexComputerUseEvidenceError("official Codex events are empty")
    return events


def _semantic_tool(code: str) -> tuple[str, tuple[str, ...]]:
    tools = tuple(_SKY_CALL.findall(code))
    if not tools:
        raise CodexComputerUseEvidenceError(
            "official node_repl call contains no @oai/sky operation"
        )
    unknown = sorted(set(tools) - set(COMPUTER_USE_TOOLS))
    if unknown:
        raise CodexComputerUseEvidenceError(
            f"official node_repl call contains unknown @oai/sky operations: {unknown!r}"
        )
    return (tools[0] if len(tools) == 1 else "batch"), tools


def summarize_events(event_path: str | Path) -> dict[str, Any]:
    """Return reproducible, non-sensitive telemetry from official Codex events."""

    calls = []
    for event in _events(Path(event_path)):
        item = event.get("item")
        if (
            event.get("type") != "item.completed"
            or not isinstance(item, dict)
            or item.get("type") != "mcp_tool_call"
        ):
            continue
        if item.get("server") != "node_repl" or item.get("tool") != "js":
            raise CodexComputerUseEvidenceError(
                "official Computer Use profile emitted a non-node_repl MCP call"
            )
        arguments = item.get("arguments")
        result = item.get("result")
        metadata = result.get("_meta") if isinstance(result, dict) else None
        surface = metadata.get("codex/toolSurface") if isinstance(metadata, dict) else None
        duration_ms = (
            metadata.get("codex/nodeReplExecutionDurationMs")
            if isinstance(metadata, dict)
            else None
        )
        if (
            not isinstance(arguments, dict)
            or not isinstance(arguments.get("code"), str)
            or not isinstance(result, dict)
            or not isinstance(surface, dict)
            or surface.get("kind") != "computerUse"
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or duration_ms < 0
            or item.get("status") != "completed"
            or item.get("error") is not None
        ):
            raise CodexComputerUseEvidenceError(
                f"official Computer Use item {item.get('id')!r} lacks successful surface evidence"
            )
        tool, semantic_tools = _semantic_tool(arguments["code"])
        content = result.get("content")
        visible_bytes = 0
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    visible_bytes += len(part["text"].encode("utf-8"))
        calls.append({
            "tool": tool,
            "semantic_tools": list(semantic_tools),
            "duration_ms": float(duration_ms),
            "request_bytes": len(_canonical_bytes(arguments)),
            "response_bytes": len(_canonical_bytes(result)),
            "model_visible_text_bytes": visible_bytes,
        })
    if not calls:
        raise CodexComputerUseEvidenceError(
            "official Codex run contains no completed Computer Use calls"
        )
    return {
        "schema_version": "openbench.codex-computer-use-telemetry.v1",
        "call_count": len(calls),
        "total_execution_ms": sum(call["duration_ms"] for call in calls),
        "total_request_bytes": sum(call["request_bytes"] for call in calls),
        "total_response_bytes": sum(call["response_bytes"] for call in calls),
        "total_model_visible_text_bytes": sum(
            call["model_visible_text_bytes"] for call in calls
        ),
        "calls": calls,
    }
