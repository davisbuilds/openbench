#!/usr/bin/env python3
"""Run OpenBench evaluators against a local LangSmith-format JSONL dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluators import checker_verdict_agreement


def _load(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def replay_outputs(example: dict[str, Any]) -> dict[str, Any]:
    """Offline target stub: replay the recorded outputs unchanged."""
    return dict(example.get("outputs") or {})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    examples = _load(args.dataset)
    results: list[dict[str, Any]] = []
    for example in examples:
        run = {"outputs": replay_outputs(example)}
        result = checker_verdict_agreement(run, example)
        results.append(result)
    passed = sum(result["score"] == 1 for result in results)
    print(json.dumps({"examples": len(results), "passed": passed}, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
