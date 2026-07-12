#!/usr/bin/env python3
"""Probe one harness' token reporting against a LiteLLM bridge usage log.

This is intentionally standalone: it does not import the OpenBench adapters, so it
can be used when a CLI updates to capture a fresh stream and compare it with the
bridge-side vendor usage JSONL produced by the probe callback documented in
TOKEN_PARITY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROMPT = (
    "In this directory: create hello.txt containing exactly hi, then read it back, "
    "then say done. Keep the final answer short."
)

DEFAULT_BRIDGE_CALL_TYPE = {
    "pi": "acompletion",
    "opencode": "acompletion",
    "claude": "anthropic_messages",
    "codex": "aresponses",
}


def _json_lines(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _json_object(path: Path) -> dict[str, Any]:
    """Load a single JSON object from a file that may include stderr noise."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object found in {path}")


def _empty(raw: Any = None) -> dict[str, Any]:
    return {
        "tokens_input_uncached": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "tokens_output": 0,
        "tokens_reasoning": 0,
        "usage_raw": raw,
    }


def _has_fields(obj: Any, fields: set[str]) -> bool:
    return isinstance(obj, dict) and fields.issubset(obj.keys())


def parse_cli(harness: str, stream: Path) -> dict[str, Any]:
    if harness == "pi":
        required = {"input", "cacheRead", "cacheWrite", "output", "reasoning"}
        usages = []
        for e in _json_lines(stream):
            if e.get("type") != "turn_end":
                continue
            usage = e.get("message", {}).get("usage")
            if _has_fields(usage, required):
                usages.append(usage)
        if not usages:
            raise ValueError(f"no complete pi turn_end usage records found in {stream}")
        out = _empty(usages)
        for u in usages:
            out["tokens_input_uncached"] += int(u.get("input") or 0)
            out["tokens_cache_read"] += int(u.get("cacheRead") or 0)
            out["tokens_cache_write"] += int(u.get("cacheWrite") or 0)
            out["tokens_output"] += int(u.get("output") or 0)  # DeepSeek completion_tokens, reasoning-inclusive
            out["tokens_reasoning"] += int(u.get("reasoning") or 0)
        out["token_basis"] = "vendor_split"
        return out

    if harness == "opencode":
        required = {"input", "output", "reasoning", "cache"}
        toks = []
        for e in _json_lines(stream):
            if e.get("type") != "step_finish":
                continue
            tokens = e.get("part", {}).get("tokens")
            cache = tokens.get("cache") if isinstance(tokens, dict) else None
            if _has_fields(tokens, required) and _has_fields(cache, {"read", "write"}):
                toks.append(tokens)
        if not toks:
            raise ValueError(f"no complete opencode step_finish token records found in {stream}")
        out = _empty(toks)
        visible_output = 0
        for t in toks:
            reasoning = int(t.get("reasoning") or 0)
            visible = int(t.get("output") or 0)
            cache = t.get("cache") or {}
            out["tokens_input_uncached"] += int(t.get("input") or 0)
            out["tokens_cache_read"] += int(cache.get("read") or 0)
            out["tokens_cache_write"] += int(cache.get("write") or 0)
            out["tokens_output"] += visible + reasoning  # normalize to DeepSeek completion_tokens
            out["tokens_reasoning"] += reasoning
            visible_output += visible
        out["opencode_visible_output_tokens"] = visible_output
        out["token_basis"] = "vendor_split"
        return out

    if harness == "codex":
        required = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}
        usages = []
        for e in _json_lines(stream):
            if e.get("type") != "turn.completed":
                continue
            usage = e.get("usage")
            if _has_fields(usage, required):
                usages.append(usage)
        if not usages:
            raise ValueError(f"no complete codex turn.completed usage records found in {stream}")
        out = _empty(usages)
        for u in usages:
            inp = int(u.get("input_tokens") or 0)
            cached = int(u.get("cached_input_tokens") or 0)
            out["tokens_input_uncached"] += inp - cached
            out["tokens_cache_read"] += cached
            out["tokens_output"] += int(u.get("output_tokens") or 0)
            out["tokens_reasoning"] += int(u.get("reasoning_output_tokens") or 0)
        out["token_basis"] = "vendor_split"
        return out

    if harness == "claude":
        obj = _json_object(stream)
        usage = obj.get("usage") or {}
        model_usage = obj.get("modelUsage")
        out = _empty({"usage": usage, "modelUsage": model_usage})
        if isinstance(model_usage, dict) and model_usage:
            required = {"inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"}
            found = False
            for item in model_usage.values():
                if not _has_fields(item, required):
                    continue
                found = True
                out["tokens_input_uncached"] += int(item.get("inputTokens") or 0)
                out["tokens_cache_read"] += int(item.get("cacheReadInputTokens") or 0)
                out["tokens_cache_write"] += int(item.get("cacheCreationInputTokens") or 0)
                out["tokens_output"] += int(item.get("outputTokens") or 0)
            if not found:
                raise ValueError(f"no complete claude modelUsage records found in {stream}")
        else:
            required = {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
            if not _has_fields(usage, required):
                raise ValueError(f"no complete claude usage record found in {stream}")
            out["tokens_input_uncached"] = int(usage.get("input_tokens") or 0)
            out["tokens_cache_read"] = int(usage.get("cache_read_input_tokens") or 0)
            out["tokens_cache_write"] = int(usage.get("cache_creation_input_tokens") or 0)
            out["tokens_output"] = int(usage.get("output_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        # Claude Code JSON currently does not expose DeepSeek reasoning token counts.
        out["tokens_reasoning"] = int(details["reasoning_tokens"]) if isinstance(details, dict) and details.get("reasoning_tokens") is not None else None
        out["token_basis"] = "vendor_split"
        return out

    raise ValueError(f"unknown harness: {harness}")


def _cli_completion_sequence(harness: str, cli: dict[str, Any]) -> list[int] | None:
    """Per-call vendor completion token sequence when the CLI exposes it.

    This lets one shared bridge usage log be replayed without accidentally
    comparing a harness against another harness' records or opencode's title
    generation call. Return None for aggregate-only CLI surfaces.
    """
    raw = cli.get("usage_raw")
    if harness == "pi" and isinstance(raw, list):
        return [int(u.get("output") or 0) for u in raw if isinstance(u, dict)]
    if harness == "opencode" and isinstance(raw, list):
        seq: list[int] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            seq.append(int(item.get("output") or 0) + int(item.get("reasoning") or 0))
        return seq
    return None


def _select_matching_records(records: list[dict[str, Any]], sequence: list[int] | None) -> list[dict[str, Any]]:
    if not sequence:
        return records
    selected: list[dict[str, Any]] = []
    pos = 0
    for rec in records:
        usage = rec.get("usage") or {}
        if int(usage.get("completion_tokens") or 0) == sequence[pos]:
            selected.append(rec)
            pos += 1
            if pos == len(sequence):
                return selected
    raise ValueError(f"could not find bridge records matching CLI completion sequence {sequence!r}")


def parse_bridge_usage(path: Path, *, since: float = 0.0, call_type: str | None = None,
                       completion_sequence: list[int] | None = None) -> dict[str, Any]:
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("ts", 0) < since:
            continue
        if call_type and rec.get("call_type") != call_type:
            continue
        records.append(rec)
    considered = len(records)
    if not records:
        raise ValueError(f"no bridge usage records matched call_type={call_type!r} in {path}")
    records = _select_matching_records(records, completion_sequence)
    if not records:
        raise ValueError(f"no bridge usage records remained after sequence matching in {path}")
    usages = []
    for rec in records:
        usage = rec.get("usage")
        if not _has_fields(usage, {"prompt_tokens", "completion_tokens", "prompt_tokens_details", "completion_tokens_details"}):
            raise ValueError(f"bridge record missing required usage fields in {path}")
        pdetails = usage.get("prompt_tokens_details")
        cdetails = usage.get("completion_tokens_details")
        if not _has_fields(pdetails, {"cached_tokens"}) or not _has_fields(cdetails, {"reasoning_tokens"}):
            raise ValueError(f"bridge record missing required usage detail fields in {path}")
        usages.append(usage)
    out = _empty(usages)
    for u in usages:
        prompt = int(u.get("prompt_tokens") or 0)
        pdetails = u.get("prompt_tokens_details") or {}
        cached = int(pdetails.get("cached_tokens") or u.get("cache_read_input_tokens") or 0)
        out["tokens_input_uncached"] += prompt - cached
        out["tokens_cache_read"] += cached
        out["tokens_cache_write"] += int(u.get("cache_creation_input_tokens") or pdetails.get("cache_creation_tokens") or 0)
        out["tokens_output"] += int(u.get("completion_tokens") or 0)
        cdetails = u.get("completion_tokens_details") or {}
        out["tokens_reasoning"] += int(cdetails.get("reasoning_tokens") or 0)
    out["records"] = len(records)
    out["records_considered"] = considered
    if completion_sequence:
        out["matched_completion_tokens"] = completion_sequence
    out["token_basis"] = "vendor_split"
    return out


def diff(cli: dict[str, Any], bridge: dict[str, Any]) -> dict[str, int | None]:
    keys = ["tokens_input_uncached", "tokens_cache_read", "tokens_cache_write", "tokens_output", "tokens_reasoning"]
    result: dict[str, int | None] = {}
    for key in keys:
        cli_value = cli.get(key)
        bridge_value = bridge.get(key)
        result[key] = None if cli_value is None or bridge_value is None else int(cli_value) - int(bridge_value)
    return result


def _pi_extension(path: Path, bridge_url: str) -> None:
    path.write_text(
        f'''export default function (pi) {{
  pi.registerProvider("deepseek_probe", {{
    name: "DeepSeek Probe Bridge",
    baseUrl: "{bridge_url}/v1",
    apiKey: "$DEEPSEEK_API_KEY",
    api: "openai-completions",
    models: [{{ id: "deepseek-v4-flash", name: "deepseek-v4-flash", reasoning: true, input: ["text"],
      compat: {{supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, thinkingFormat: "deepseek", requiresReasoningContentOnAssistantMessages: true}},
      thinkingLevelMap: {{off: null}}, cost: {{ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }}, contextWindow: 128000, maxTokens: 8192 }}]
  }});
}}
''',
        encoding="utf-8",
    )


def _agent_env(home: Path) -> dict[str, str]:
    """Minimal child env for autonomous CLIs; never inherit host secrets."""
    keep = {
        "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL",
        "USER", "LOGNAME",
    }
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["HOME"] = str(home)
    env["DEEPSEEK_API_KEY"] = "openbench-bridge-placeholder"
    return env


def run_harness(harness: str, out: Path, bridge_url: str, prompt: str) -> int:
    work = Path(tempfile.mkdtemp(prefix=f"parity_{harness}_work_"))
    home = Path(tempfile.mkdtemp(prefix=f"parity_{harness}_home_"))
    try:
        env = _agent_env(home)
        if harness == "pi":
            ext = home / "open-provider.mjs"
            _pi_extension(ext, bridge_url)
            cmd = ["pi", "-p", "--no-extensions", "-e", str(ext), "--provider", "deepseek_probe", "--model", "deepseek-v4-flash", "--thinking", "medium", "--mode", "json", prompt]
        elif harness == "opencode":
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"provider": {"deepseek_probe": {"npm": "@ai-sdk/openai-compatible", "name": "DeepSeek Probe Bridge", "options": {"baseURL": bridge_url + "/v1", "apiKey": "{env:DEEPSEEK_API_KEY}"}, "models": {"deepseek-v4-flash": {}}}}})
            cmd = ["opencode", "run", "--dir", str(work), "-m", "deepseek_probe/deepseek-v4-flash", "--variant", "medium", "--auto", "--format", "json", prompt]
        elif harness == "codex":
            cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "-C", str(work), "-s", "workspace-write", "-c", 'model_providers.deepseek.name="DeepSeek Probe Bridge"', "-c", f'model_providers.deepseek.base_url="{bridge_url}/v1"', "-c", 'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"', "-c", 'model_providers.deepseek.wire_api="responses"', "-c", 'model_provider="deepseek"', "-c", 'model_reasoning_effort="medium"', "-m", "deepseek-v4-flash", prompt]
        elif harness == "claude":
            env.update({"CLAUDE_CONFIG_DIR": str(home / ".claude"), "ANTHROPIC_BASE_URL": bridge_url, "ANTHROPIC_API_KEY": "openbench-bridge-placeholder", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_BUG_COMMAND": "1", "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1"})
            cmd = ["claude", "-p", "--bare", "--output-format", "json", "--model", "deepseek-v4-flash", "--effort", "medium", "--dangerously-skip-permissions", "--no-session-persistence", prompt]
        else:
            raise ValueError(harness)
        proc = subprocess.run(cmd, cwd=work, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=180)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        return proc.returncode
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", required=True, choices=["pi", "opencode", "claude", "codex"])
    ap.add_argument("--stream", type=Path, help="Existing CLI stream to parse, or output path when running")
    ap.add_argument("--bridge-usage-log", type=Path, help="JSONL written by usage_logger.usage_logger")
    ap.add_argument("--bridge-call-type", choices=["acompletion", "anthropic_messages", "aresponses"], help="Filter bridge records by LiteLLM call_type")
    ap.add_argument("--run", action="store_true", help="Run the harness against --bridge-url before parsing")
    ap.add_argument("--bridge-url", default="http://127.0.0.1:4242")
    ap.add_argument("--prompt", default=PROMPT)
    args = ap.parse_args()

    stream = args.stream or Path(f"{args.harness}-stream.txt")
    since = time.time()
    run_rc = 0
    if args.run:
        run_rc = run_harness(args.harness, stream, args.bridge_url.rstrip("/"), args.prompt)
    try:
        cli = parse_cli(args.harness, stream)
    except Exception as exc:  # noqa: BLE001 - probe should report captured stream path cleanly
        result = {"harness": args.harness, "error": f"failed to parse {stream}: {exc}"}
        if run_rc != 0:
            result["error"] = f"harness exited {run_rc}; stream captured at {stream}; parse failed: {exc}"
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    result: dict[str, Any] = {"harness": args.harness, "cli": cli}
    if run_rc != 0:
        result["error"] = f"harness exited {run_rc}; stream captured at {stream}"
    if args.bridge_usage_log:
        call_type = args.bridge_call_type or DEFAULT_BRIDGE_CALL_TYPE[args.harness]
        sequence = _cli_completion_sequence(args.harness, cli)
        bridge = parse_bridge_usage(
            args.bridge_usage_log,
            since=since if args.run else 0.0,
            call_type=call_type,
            completion_sequence=sequence,
        )
        result["bridge_call_type"] = call_type
        result["bridge"] = bridge
        result["cli_minus_bridge"] = diff(cli, bridge)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if run_rc != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
