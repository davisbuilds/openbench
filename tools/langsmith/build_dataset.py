#!/usr/bin/env python3
"""Build upload-ready LangSmith examples from OpenBench result rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_RESULTS = "data/gpt56-2026-07-21/gpt56-final-7arms.jsonl"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                message = f"{path}:{line_number}: invalid JSON: {exc}"
                raise ValueError(message) from exc
            if not isinstance(row, dict):
                message = f"{path}:{line_number}: result row is not an object"
                raise ValueError(message)
            rows.append(row)
    return rows


def _instruction(tasks_dir: Path, task: Any) -> str:
    if not isinstance(task, str) or not task:
        return ""
    instruction_path = tasks_dir / task / "instruction.md"
    try:
        return instruction_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def example_from_row(row: dict[str, Any], tasks_dir: Path) -> dict[str, Any]:
    """Convert one OpenBench result row to the dataset example schema."""
    task = row.get("task")
    return {
        "inputs": {
            "task": task,
            "instruction": _instruction(tasks_dir, task),
            "harness": row.get("harness"),
            "model": row.get("model"),
            "trial": row.get("trial"),
        },
        "outputs": {
            "success": row.get("success"),
            "score": row.get("score"),
            "checker_exit": row.get("checker_exit"),
            "checker_stdout": row.get("checker_stdout"),
            "failure_class": row.get("failure_class"),
        },
        "metadata": {
            "run_id": row.get("run_id"),
            "tokens": row.get("tokens"),
            "wall_time_s": row.get("wall_time_s"),
        },
    }


def build_dataset(results: Path, tasks_dir: Path) -> list[dict[str, Any]]:
    return [example_from_row(row, tasks_dir) for row in _read_rows(results)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build LangSmith dataset JSONL from OpenBench results."
    )
    parser.add_argument("--results", default=DEFAULT_RESULTS, type=Path)
    parser.add_argument("--tasks-dir", default="tasks", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    examples = build_dataset(args.results, args.tasks_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False) + "\n")
    print(f"wrote {len(examples)} examples to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
