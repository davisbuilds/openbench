#!/usr/bin/env python3
"""Convert OpenBench raw transcripts into ATIF trajectories.

Raw transcripts remain the source of truth. ATIF files are derived artifacts;
scrub and review them before sharing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from obench.atif import SCHEMA_VERSION, assert_valid_trajectory, dump_trajectory, to_dict

HEADER_RE = re.compile(r"^# harness=(?P<harness>\S+) model=(?P<model>\S+) task=(?P<task>\S+) trial=(?P<trial>\d+)")
RUN_RE = re.compile(r"^# transcript (?P<harness>[^:]+):(?P<task>.+):(?P<model>[^:]+):trial(?P<trial>\d+)")


def _num(value: Any, default: int | None = None) -> int | None:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"token count must be integral, got {value!r}")
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError as exc:
            raise ValueError(f"token count must be integral, got {value!r}") from exc
    return default


def _read_transcript(path: Path) -> tuple[dict[str, Any], list[Any]]:
    lines = path.read_text().splitlines()
    meta: dict[str, Any] = {"path": str(path), "stem": path.stem}
    for line in lines[:4]:
        m = HEADER_RE.match(line)
        if m:
            meta.update(m.groupdict())
            meta["trial"] = int(meta["trial"])
        m = RUN_RE.match(line)
        if m:
            meta.setdefault("run_id", line.removeprefix("# transcript "))
            for k, v in m.groupdict().items():
                meta.setdefault(k, int(v) if k == "trial" else v)
    if "harness" not in meta:
        meta["harness"] = path.name.split("_", 1)[0]
    events: list[Any] = []
    for line_number, line in enumerate(lines[3:], start=4):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("Reading additional input"):
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: malformed JSON event: {exc}") from exc
    if not events:
        raise ValueError(f"{path}: transcript contains no JSON events")
    return meta, events


def _split_content(content: Any) -> tuple[str, str | None, list[dict[str, Any]]]:
    if isinstance(content, str):
        return content, None, []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                typ = part.get("type")
                if typ in {"thinking", "reasoning", "analysis"}:
                    val = part.get("thinking") or part.get("text")
                    if isinstance(val, str):
                        reasoning_parts.append(val)
                elif typ == "text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif typ in {"toolCall", "tool_use"}:
                    call_id = part.get("id") or part.get("tool_call_id") or part.get("tool_use_id")
                    name = part.get("name") or part.get("function_name")
                    args = part.get("arguments") if "arguments" in part else part.get("input")
                    if not isinstance(args, dict):
                        args = {"value": args} if args is not None else {}
                    if call_id and name:
                        tool_calls.append(
                            {"tool_call_id": str(call_id), "function_name": str(name), "arguments": args}
                        )
                elif "text" in part and isinstance(part["text"], str):
                    text_parts.append(part["text"])
                else:
                    text_parts.append(json.dumps(part, ensure_ascii=False))
            else:
                text_parts.append(str(part))
    elif content is not None:
        text_parts.append(str(content))
    text = "\n\n".join(p.strip() for p in text_parts if str(p).strip())
    reasoning = "\n\n".join(p.strip() for p in reasoning_parts if str(p).strip())
    return text, reasoning or None, tool_calls


def _text_from_content(content: Any) -> tuple[str, str | None]:
    text, reasoning, _tool_calls = _split_content(content)
    return text, reasoning


def _observation_content(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            text, _ = _text_from_content(content)
            return text
        if content is not None:
            return str(content)
        return json.dumps(result, ensure_ascii=False)
    return "" if result is None else str(result)


def _metric_from_openbench_usage(usage: dict[str, Any], *, prompt_includes_cache: bool) -> dict[str, Any]:
    if prompt_includes_cache:
        prompt = _num(usage.get("input_tokens"), 0) or 0
        cached = _num(usage.get("cached_input_tokens"), 0) or 0
        completion = _num(usage.get("output_tokens"), 0) or 0
        reasoning = _num(usage.get("reasoning_output_tokens"), 0)
        extra = {"reasoning_tokens": reasoning} if reasoning is not None else {}
        return {"prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached, "extra": extra}

    inp = _num(usage.get("input"), 0) or 0
    cached = _num(usage.get("cacheRead"), 0) or 0
    cache_write = _num(usage.get("cacheWrite"), 0) or 0
    completion = _num(usage.get("output"), 0) or 0
    reasoning = _num(usage.get("reasoning"), 0)
    cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
    total_cost = cost.get("total") if isinstance(cost, dict) else None
    extra = {"cache_write_tokens": cache_write}
    if reasoning is not None:
        extra["reasoning_tokens"] = reasoning
    if usage.get("totalTokens") is not None:
        extra["total_tokens"] = usage.get("totalTokens")
    return {
        "prompt_tokens": inp + cached + cache_write,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cost_usd": float(total_cost) if isinstance(total_cost, (int, float)) else None,
        "extra": extra,
    }


def _final_metrics(steps: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cached_tokens": 0,
        "total_steps": len(steps),
        "extra": {"cache_write_tokens": 0, "reasoning_tokens": 0},
    }
    cost = 0.0
    saw_cost = False
    for step in steps:
        m = step.get("metrics") or {}
        out["total_prompt_tokens"] += int(m.get("prompt_tokens") or 0)
        out["total_completion_tokens"] += int(m.get("completion_tokens") or 0)
        out["total_cached_tokens"] += int(m.get("cached_tokens") or 0)
        extra = m.get("extra") or {}
        out["extra"]["cache_write_tokens"] += int(extra.get("cache_write_tokens") or extra.get("cache_creation_input_tokens") or 0)
        out["extra"]["reasoning_tokens"] += int(extra.get("reasoning_tokens") or extra.get("reasoning_output_tokens") or 0)
        if m.get("cost_usd") is not None:
            cost += float(m["cost_usd"])
            saw_cost = True
    if saw_cost:
        out["total_cost_usd"] = cost
    return out


def _base(meta: dict[str, Any], name: str, events: list[Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": meta.get("run_id") or meta.get("stem"),
        "agent": {"name": name, "version": "unknown", "model_name": meta.get("model")},
        "steps": [],
        "extra": {"source_transcript": meta.get("path"), "task": meta.get("task"), "trial": meta.get("trial"), "event_count": len(events)},
    }


def convert_codex(path: Path) -> dict[str, Any]:
    meta, events = _read_transcript(path)
    traj = _base(meta, "codex", events)
    thread_id = next((e.get("thread_id") for e in events if isinstance(e, dict) and e.get("type") == "thread.started"), None)
    if thread_id:
        traj["session_id"] = thread_id
    steps: list[dict[str, Any]] = []
    pending_agent_step_indexes: list[int] = []
    started_item_steps: dict[str, int] = {}
    previous_usage: dict[str, Any] | None = None

    def usage_delta(current: dict[str, Any]) -> dict[str, Any]:
        nonlocal previous_usage
        if previous_usage is None:
            previous_usage = current
            return current
        delta = dict(current)
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cache_creation_tokens",
        ):
            cur = _num(current.get(key))
            prev = _num(previous_usage.get(key))
            if cur is not None and prev is not None and cur >= prev:
                delta[key] = cur - prev
        previous_usage = current
        return delta

    def build_codex_tool_step(item: dict[str, Any], *, incomplete: bool, step_id: int) -> dict[str, Any]:
        typ = item.get("type")
        call_id = item.get("id") or f"tool_{step_id}"
        if typ == "command_execution":
            result = {
                "source_call_id": call_id,
                "content": item.get("aggregated_output") or "",
                "extra": {
                    "status": "incomplete" if incomplete else item.get("status"),
                    "exit_code": item.get("exit_code"),
                    "incomplete": incomplete,
                },
            }
            return {
                "step_id": step_id,
                "source": "agent",
                "message": "",
                "llm_call_count": 0,
                "tool_calls": [
                    {
                        "tool_call_id": call_id,
                        "function_name": "command_execution",
                        "arguments": {"command": item.get("command") or ""},
                    }
                ],
                "observation": {"results": [result]},
            }
        if typ == "file_change":
            status = "incomplete" if incomplete else item.get("status") or "completed"
            result = {
                "source_call_id": call_id,
                "content": status,
                "extra": {"status": status, "incomplete": incomplete},
            }
            return {
                "step_id": step_id,
                "source": "agent",
                "message": "",
                "llm_call_count": 0,
                "tool_calls": [
                    {
                        "tool_call_id": call_id,
                        "function_name": "file_change",
                        "arguments": {"changes": item.get("changes") or []},
                    }
                ],
                "observation": {"results": [result]},
            }
        raise ValueError(f"unsupported codex tool item type: {typ!r}")

    def append_or_update_codex_tool_step(item: dict[str, Any], *, incomplete: bool) -> None:
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id in started_item_steps:
            index = started_item_steps.pop(item_id)
            steps[index] = build_codex_tool_step(item, incomplete=incomplete, step_id=steps[index]["step_id"])
            return
        steps.append(build_codex_tool_step(item, incomplete=incomplete, step_id=len(steps) + 1))
        if incomplete and isinstance(item_id, str):
            started_item_steps[item_id] = len(steps) - 1

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if etype == "item.started" and item:
            if item.get("type") in {"command_execution", "file_change"}:
                append_or_update_codex_tool_step(item, incomplete=True)
            continue
        if etype == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = usage_delta(event["usage"])
            if pending_agent_step_indexes:
                steps[pending_agent_step_indexes[-1]]["metrics"] = _metric_from_openbench_usage(
                    usage, prompt_includes_cache=True
                )
                pending_agent_step_indexes.clear()
            continue
        if etype != "item.completed" or not item:
            continue
        typ = item.get("type")
        if typ == "agent_message":
            step = {"step_id": len(steps) + 1, "source": "agent", "message": item.get("text") or "", "llm_call_count": 1, "extra": {"codex_item_id": item.get("id")}}
            steps.append(step)
            pending_agent_step_indexes.append(len(steps) - 1)
        elif typ in {"command_execution", "file_change"}:
            append_or_update_codex_tool_step(item, incomplete=False)
    if not steps:
        raise ValueError(f"{path}: no recognized codex trajectory events")
    traj["steps"] = steps
    traj["final_metrics"] = _final_metrics(traj["steps"])
    return traj


def convert_pi(path: Path) -> dict[str, Any]:
    meta, events = _read_transcript(path)
    traj = _base(meta, "pi", events)
    session = next((e for e in events if isinstance(e, dict) and e.get("type") == "session"), None)
    if isinstance(session, dict):
        traj["session_id"] = session.get("id") or traj["session_id"]
        traj["agent"]["version"] = str(session.get("version", "unknown"))
        traj["extra"]["cwd"] = session.get("cwd")

    turn_usages = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "turn_end":
            continue
        message = event.get("message") or {}
        usage = message.get("usage") if isinstance(message, dict) else None
        turn_usages.append(usage if isinstance(usage, dict) else None)
    use_turn_usages = any(isinstance(usage, dict) for usage in turn_usages)
    assistant_index = 0
    steps: list[dict[str, Any]] = []
    observed_tool_ids: set[str] = set()
    emitted_tool_ids: set[str] = set()

    tool_starts: dict[str, dict[str, Any]] = {
        event.get("toolCallId", ""): event
        for event in events
        if isinstance(event, dict) and event.get("type") == "tool_execution_start"
    }
    tool_ends: dict[str, dict[str, Any]] = {
        event.get("toolCallId", ""): event
        for event in events
        if isinstance(event, dict) and event.get("type") == "tool_execution_end"
    }

    def observation_from_tool_end(call_id: str, end: dict[str, Any] | None) -> dict[str, Any]:
        if end is None:
            return {
                "source_call_id": call_id,
                "content": "",
                "extra": {"status": "incomplete", "incomplete": True},
            }
        status = "error" if end.get("isError") else "success"
        return {
            "source_call_id": call_id,
            "content": _observation_content(end.get("result")),
            "extra": {"status": status, "is_error": end.get("isError")},
        }

    def attach_observation(call_id: str, result: dict[str, Any]) -> bool:
        for step in reversed(steps):
            calls = step.get("tool_calls") or []
            if any(call.get("tool_call_id") == call_id for call in calls if isinstance(call, dict)):
                step.setdefault("observation", {"results": []})["results"].append(result)
                observed_tool_ids.add(call_id)
                return True
        return False

    def _tool_step(call_id: str, start: dict[str, Any] | None) -> dict[str, Any]:
        args = start.get("args") if isinstance(start, dict) and isinstance(start.get("args"), dict) else {}
        name = start.get("toolName") if isinstance(start, dict) else None
        return {
            "step_id": len(steps) + 1,
            "source": "agent",
            "message": "",
            "llm_call_count": 0,
            "tool_calls": [
                {
                    "tool_call_id": call_id,
                    "function_name": name or "tool",
                    "arguments": args,
                }
            ],
        }

    def append_tool_start_step(call_id: str, start: dict[str, Any] | None) -> None:
        if call_id in emitted_tool_ids:
            return
        steps.append(_tool_step(call_id, start))
        emitted_tool_ids.add(call_id)

    def append_tool_step(call_id: str, start: dict[str, Any] | None, result: dict[str, Any]) -> None:
        step = _tool_step(call_id, start)
        step["observation"] = {"results": [result]}
        steps.append(step)
        emitted_tool_ids.add(call_id)
        observed_tool_ids.add(call_id)

    def append_message_step(msg: dict[str, Any]) -> None:
        nonlocal assistant_index
        role = msg.get("role")
        if role == "toolResult":
            call_id = str(msg.get("toolCallId") or f"tool_result_{len(steps) + 1}")
            content, _reasoning = _text_from_content(msg.get("content"))
            status = "error" if msg.get("isError") else "success"
            result = {
                "source_call_id": call_id,
                "content": content,
                "extra": {
                    "status": status,
                    "is_error": msg.get("isError"),
                    "tool_name": msg.get("toolName"),
                },
            }
            if not attach_observation(call_id, result):
                append_tool_step(call_id, tool_starts.get(call_id), result)
            return

        content, reasoning, tool_calls = _split_content(msg.get("content"))
        source = "agent" if role == "assistant" else "user" if role == "user" else "system"
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": source,
            "message": content,
        }
        if source == "agent":
            step["llm_call_count"] = 1
            if tool_calls:
                step["tool_calls"] = tool_calls
                for call in tool_calls:
                    emitted_tool_ids.add(call["tool_call_id"])
            if msg.get("model"):
                step["model_name"] = msg.get("model")
                traj["agent"]["model_name"] = traj["agent"].get("model_name") or msg.get("model")
            if reasoning:
                step["reasoning_content"] = reasoning
            usage = None
            if use_turn_usages:
                if assistant_index < len(turn_usages):
                    usage = turn_usages[assistant_index]
            elif isinstance(msg.get("usage"), dict):
                usage = msg["usage"]
            if isinstance(usage, dict):
                step["metrics"] = _metric_from_openbench_usage(usage, prompt_includes_cache=False)
            step["extra"] = {
                k: msg[k]
                for k in ("api", "provider", "stopReason", "responseId")
                if k in msg
            }
            assistant_index += 1
        steps.append(step)

    agent_end_messages = None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "agent_end" and isinstance(event.get("messages"), list):
            agent_end_messages = event["messages"]
    if agent_end_messages is not None:
        for msg in agent_end_messages:
            if isinstance(msg, dict):
                append_message_step(msg)
    else:
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "message_end" and isinstance(event.get("message"), dict):
                append_message_step(event["message"])
            elif event.get("type") == "tool_execution_start":
                call_id = str(event.get("toolCallId") or f"tool_{len(steps) + 1}")
                append_tool_start_step(call_id, event)
            elif event.get("type") == "tool_execution_end":
                call_id = str(event.get("toolCallId") or f"tool_{len(steps) + 1}")
                result = observation_from_tool_end(call_id, event)
                if not attach_observation(call_id, result):
                    append_tool_step(call_id, tool_starts.get(call_id), result)

    for call_id, start in tool_starts.items():
        if call_id in observed_tool_ids:
            continue
        result = observation_from_tool_end(call_id, tool_ends.get(call_id))
        if not attach_observation(call_id, result):
            append_tool_step(call_id, start, result)

    if not steps:
        raise ValueError(f"{path}: no recognized pi trajectory events")
    for index, step in enumerate(steps, 1):
        step["step_id"] = index
    traj["steps"] = steps
    traj["final_metrics"] = _final_metrics(traj["steps"])
    return traj


def convert_claude(path: Path) -> dict[str, Any]:
    meta, events = _read_transcript(path)
    traj = _base(meta, "claude", events)
    obj = next((e for e in events if isinstance(e, dict) and ("result" in e or "usage" in e or "modelUsage" in e)), {})
    if not obj:
        raise ValueError(f"{path}: no recognized claude result event")
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
    model_usage = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else None
    steps: list[dict[str, Any]] = []
    metrics: dict[str, Any] | None = None
    if model_usage:
        prompt = completion = cached = cache_write = 0
        cost = 0.0
        saw_cost = False
        for mu in model_usage.values():
            if not isinstance(mu, dict):
                continue
            inp = _num(mu.get("inputTokens"), 0) or 0
            out = _num(mu.get("outputTokens"), 0) or 0
            cr = _num(mu.get("cacheReadInputTokens"), 0) or 0
            cw = _num(mu.get("cacheCreationInputTokens"), 0) or 0
            prompt += inp + cr + cw
            completion += out
            cached += cr
            cache_write += cw
            if isinstance(mu.get("costUSD"), (int, float)):
                cost += float(mu["costUSD"])
                saw_cost = True
        metrics = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "extra": {"cache_write_tokens": cache_write},
        }
        if saw_cost:
            metrics["cost_usd"] = cost
    elif usage:
        inp = _num(usage.get("input_tokens"), 0) or 0
        out = _num(usage.get("output_tokens"), 0) or 0
        cr = _num(usage.get("cache_read_input_tokens"), 0) or 0
        cw = _num(usage.get("cache_creation_input_tokens"), 0) or 0
        metrics = {"prompt_tokens": inp + cr + cw, "completion_tokens": out, "cached_tokens": cr, "extra": {"cache_write_tokens": cw}}
    steps.append({"step_id": 1, "source": "agent", "message": str(obj.get("result") or ""), "llm_call_count": obj.get("num_turns") or 1})
    if metrics:
        steps[0]["metrics"] = metrics
    if obj:
        traj["extra"].update({k: obj.get(k) for k in ("subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns") if k in obj})
    traj["steps"] = steps
    traj["final_metrics"] = _final_metrics(steps)
    return traj


CONVERTERS = {"codex": convert_codex, "pi": convert_pi, "claude": convert_claude}


def convert_file(path: Path, harness: str = "auto") -> dict[str, Any]:
    meta, _ = _read_transcript(path)
    selected = meta.get("harness") if harness == "auto" else harness
    if selected not in CONVERTERS:
        raise ValueError(f"unsupported harness {selected!r}; expected one of {sorted(CONVERTERS)}")
    traj = CONVERTERS[selected](path)
    assert_valid_trajectory(to_dict(traj))
    return to_dict(traj)


def _inputs(paths: list[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(path.glob("*.txt"))
        else:
            yield path


def _out_path(src: Path, out_dir: Path | None) -> Path:
    if out_dir is not None:
        return out_dir / f"{src.stem}.trajectory.json"
    return src.with_name(f"{src.stem}.trajectory.json")


def _planned_outputs(inputs: list[Path], out_dir: Path | None) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    seen: dict[Path, Path] = {}
    for src in _inputs(inputs):
        dest = _out_path(src, out_dir)
        if dest in seen:
            raise ValueError(f"duplicate output path {dest} for {seen[dest]} and {src}")
        seen[dest] = src
        pairs.append((src, dest))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert OpenBench transcripts to ATIF JSON")
    parser.add_argument("inputs", nargs="+", type=Path, help="transcript file(s) or transcript directory")
    parser.add_argument("--harness", default="auto", choices=["auto", *sorted(CONVERTERS)], help="harness converter to use")
    parser.add_argument("--out", type=Path, default=None, help="output directory; defaults next to each transcript")
    parser.add_argument("--validate-only", action="store_true", help="convert and validate without writing output")
    args = parser.parse_args(argv)

    try:
        outputs = _planned_outputs(args.inputs, args.out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    count = 0
    for src, dest in outputs:
        if not src.exists():
            print(f"missing: {src}", file=sys.stderr)
            return 2
        traj = convert_file(src, args.harness)
        if not args.validate_only:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dump_trajectory(traj, dest)
            print(f"{src} -> {dest}")
        else:
            print(f"valid: {src}")
        count += 1
    print(f"converted {count} transcript(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
