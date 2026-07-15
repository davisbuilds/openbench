#!/usr/bin/env python3
"""Estimate cross-session prefix duplication from one proxy JSONL ledger.

The ledger stores usage counts and hashed conversation links, never prompt text.
Consequently the duplication result is a size-only estimate, not proof that two
prefixes have equal content. See ``method`` and ``limits`` in the JSON output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def input_split(usage: Any) -> tuple[int, int, int] | None:
    """Return (uncached, cache-read, cache-write) for known ledger schemas."""
    if not isinstance(usage, dict):
        return None
    if "totalTokens" in usage and ("input" in usage or "output" in usage):
        uncached = _number(usage.get("input"))
        if uncached is None:
            return None
        return uncached, _number(usage.get("cacheRead")) or 0, _number(usage.get("cacheWrite")) or 0
    if "input_tokens" in usage:
        total = _number(usage.get("input_tokens"))
        if total is None:
            return None
        details = usage.get("input_tokens_details") or {}
        read = _number(usage.get("cache_read_input_tokens"))
        if read is None:
            read = _number(usage.get("cached_input_tokens"))
        if read is None and isinstance(details, dict):
            read = _number(details.get("cached_tokens"))
        read = read or 0
        write = (_number(usage.get("cache_creation_input_tokens"))
                 or _number(usage.get("cache_write_tokens"))
                 or (_number(details.get("cache_write_tokens")) if isinstance(details, dict) else None)
                 or 0)
        # Anthropic's input field excludes cache lanes; OpenAI's includes them.
        anthropic = "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
        return (total, read, write) if anthropic else (max(0, total - read - write), read, write)
    if "prompt_tokens" in usage:
        total = _number(usage.get("prompt_tokens"))
        if total is None:
            return None
        details = usage.get("prompt_tokens_details") or {}
        read = _number(usage.get("prompt_cache_hit_tokens"))
        if read is None and isinstance(details, dict):
            read = _number(details.get("cached_tokens"))
        read = read or 0
        uncached = _number(usage.get("prompt_cache_miss_tokens"))
        return (max(0, total - read) if uncached is None else uncached,
                read, _number(usage.get("prompt_cache_write_tokens")) or 0)
    return None


def _session_components(rows: list[dict[str, Any]]) -> tuple[list[list[int]], int]:
    """Group calls by hashed session IDs or response/previous-response links."""
    parent = list(range(len(rows)))
    identified: set[int] = set()

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    sessions: dict[str, int] = {}
    responses: dict[str, int] = {}
    for i, row in enumerate(rows):
        session = row.get("session_hash")
        if isinstance(session, str):
            identified.add(i)
            if session in sessions:
                union(i, sessions[session])
            else:
                sessions[session] = i
        response = row.get("response_hash")
        if isinstance(response, str):
            identified.add(i)
            responses[response] = i
        previous = row.get("previous_response_hash")
        if isinstance(previous, str):
            identified.add(i)
            if previous in responses:
                identified.add(responses[previous])
                union(i, responses[previous])
    groups: dict[int, list[int]] = {}
    for i in sorted(identified):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda group: group[0]), len(rows) - len(identified)


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [row for row in rows if isinstance(row, dict) and input_split(row.get("usage")) is not None]
    splits = [input_split(row["usage"]) for row in calls]
    total_input = sum(sum(split) for split in splits if split is not None)
    components, unidentified = _session_components(calls)
    first_sizes = [sum(splits[group[0]]) for group in components]
    duplicate = 0
    if first_sizes:
        reference = first_sizes[0]
        duplicate = sum(min(reference, size) for size in first_sizes[1:])
    session_count = len(components) if unidentified == 0 else None
    return {
        "calls_with_usage": len(calls),
        "sessions_observed": session_count,
        "identified_session_components": len(components),
        "unidentified_calls": unidentified,
        "total_input_tokens": total_input,
        "duplicated_prefix_tokens_estimate": duplicate if session_count is not None else None,
        "first_request_input_tokens_by_session": first_sizes,
        "method": ("Sessions are connected by salted proxy hashes of explicit session IDs or "
                   "response/previous-response IDs. Total input is uncached + cache-read + "
                   "cache-write. For each session after the first, duplicated prefix is estimated "
                   "as min(that session's first-request input, the first session's first-request input)."),
        "limits": ("The proxy never stores prompt content, so equal token counts do not establish an "
                   "equal prefix; the duplication value is a size-only potential-overlap estimate. "
                   "Cache splits show provider reuse, not which session supplied the content. If "
                   "conversation links are absent, session count and duplication are null rather than guessed."),
    }


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {number}: {exc.msg}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", help="one cell's proxy JSONL ledger")
    args = parser.parse_args(argv)
    print(json.dumps(analyze(read_ledger(args.ledger)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
