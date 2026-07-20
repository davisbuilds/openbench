#!/usr/bin/env python3
"""Backfill TOKEN_PARITY split token fields from saved local transcripts.

The adapter modules are the source of truth for harness-specific usage parsing;
this tool only locates transcripts, invokes those parsers, writes normalized
fields, and reports self-test/reconciliation statistics.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TOKEN_FIELDS = (
    "tokens_input_uncached",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_output",
    "tokens_reasoning",
)
BACKFILL_FIELDS = TOKEN_FIELDS + ("token_basis", "tokens_fresh")
PARSER_HARNESSES = {"pi", "claude", "codex", "opencode"}
GROKBUILD = "grokbuild"
VENDOR_SPLIT = "vendor_split"
UNAVAILABLE = "unavailable"
SCALAR_EXACT = "scalar_exact"
# Lanes whose legacy 'tokens' scalar is already uncached_input + output, so the
# scalar can be adopted as tokens_fresh when the transcript is unavailable:
# pi verified exact by the parity probe (zero delta vs bridge ground truth) and
# by this tool's own self-test; grokbuild computes the scalar from per-call
# inference_done events with the same formula. claude/codex scalars use other
# bases and must NEVER be adopted.
SCALAR_EXACT_HARNESSES = {"pi", GROKBUILD}


class BackfillError(RuntimeError):
    """Fatal backfill/self-test error."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_adapter(name: str) -> Any:
    path = _repo_root() / "obench" / "adapters" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"parity_backfill_adapter_{name}", path)
    if spec is None or spec.loader is None:
        raise BackfillError(f"cannot load adapter {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _empty_usage() -> dict[str, Any]:
    return {field: None for field in TOKEN_FIELDS} | {"token_basis": UNAVAILABLE}


def _fresh_from_usage(usage: dict[str, Any]) -> int | None:
    # Delegated backfill contract: tokens_fresh is the cross-harness comparable
    # scalar used for this dataset and is exactly uncached input + output. Cache
    # writes stay in their split field and are used only for Claude legacy-scalar
    # reconciliation below, not folded into tokens_fresh.
    inp = _int_or_none(usage.get("tokens_input_uncached"))
    out = _int_or_none(usage.get("tokens_output"))
    if inp is None or out is None:
        return None
    return inp + out


def _reconcile_from_usage(usage: dict[str, Any]) -> int | None:
    inp = _int_or_none(usage.get("tokens_input_uncached"))
    out = _int_or_none(usage.get("tokens_output"))
    write = _int_or_none(usage.get("tokens_cache_write"))
    if inp is None or out is None:
        return None
    return inp + out + (write or 0)


def _is_parseable_usage(harness: str, usage: dict[str, Any]) -> bool:
    if usage.get("token_basis") != VENDOR_SPLIT:
        return False
    required = [
        "tokens_input_uncached",
        "tokens_cache_read",
        "tokens_cache_write",
        "tokens_output",
    ]
    if harness != "claude":
        required.append("tokens_reasoning")
    return all(_int_or_none(usage.get(k)) is not None for k in required)


def _usage_without_raw(usage: dict[str, Any]) -> dict[str, Any]:
    return {k: usage.get(k) for k in TOKEN_FIELDS + ("token_basis",)}


def _parse_usage(adapter: Any, harness: str, raw: str) -> tuple[dict[str, Any], str | None]:
    try:
        parsed = adapter._parse_json_with_usage(raw)  # noqa: SLF001 - intentional parser reuse
    except Exception as exc:  # noqa: BLE001 - never guess on parser failure
        return _empty_usage(), f"parser_exception:{type(exc).__name__}"

    if harness == "claude":
        if not isinstance(parsed, tuple) or len(parsed) != 5:
            return _empty_usage(), "parser_shape"
        usage = parsed[4]
    else:
        if not isinstance(parsed, tuple) or len(parsed) != 4:
            return _empty_usage(), "parser_shape"
        usage = parsed[3]

    if not isinstance(usage, dict) or not _is_parseable_usage(harness, usage):
        return _empty_usage(), "unparsable_usage"
    return _usage_without_raw(usage), None


def _task_filename_part(task: Any) -> str:
    return str(task or "").replace("/", "_")


def transcript_path(row: dict[str, Any], results_path: Path, transcripts_dir: Path) -> Path | None:
    harness = row.get("harness")
    task = _task_filename_part(row.get("task"))
    model = row.get("model")
    trial = row.get("trial")
    if not harness or not task or not model or trial is None:
        return None
    filename = f"{harness}_{task}_{model}_trial{trial}.txt"
    candidates = [
        transcripts_dir / filename,
        transcripts_dir / results_path.stem / filename,
        transcripts_dir / f"tb-open-n3-{model}" / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[1]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BackfillError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise BackfillError(f"{path}:{lineno}: expected object row")
            rows.append(row)
    return rows


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".pre-parity.bak")


def _write_jsonl(path: Path, rows: list[dict[str, Any]], original_stat: os.stat_result) -> tuple[Path, bool]:
    try:
        current = path.stat()
    except OSError as exc:
        raise BackfillError(f"cannot stat {path} before write: {exc}") from exc
    if (current.st_size, current.st_mtime_ns) != (original_stat.st_size, original_stat.st_mtime_ns):
        raise BackfillError(
            f"refusing to overwrite {path}: file changed while backfill was running"
        )

    backup = _backup_path(path)
    backup_created = False
    if not backup.exists():
        shutil.copy2(path, backup)
        backup_created = True

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return backup, backup_created


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _add_ratio(stats: dict[str, Any], old: int | None, fresh: int | None) -> None:
    if old is None or fresh is None:
        return
    stats["old_sum"] += old
    stats["fresh_sum"] += fresh
    stats["comparable_rows"] += 1
    if old:
        stats["fresh_over_old"].append(fresh / old)


def _new_lane_stats() -> dict[str, Any]:
    return {
        "rows": 0,
        "updated": 0,
        "unavailable": 0,
        "scalar_adopted": 0,
        "old_sum": 0,
        "fresh_sum": 0,
        "comparable_rows": 0,
        "fresh_over_old": [],
        "basis": Counter(),
        "unavailable_reasons": Counter(),
        "claude_reconcile_rows": 0,
        "claude_reconcile_exact": 0,
        "claude_reconcile_diffs": [],
    }


def _apply_unavailable(row: dict[str, Any]) -> None:
    for field in TOKEN_FIELDS:
        row[field] = None
    row["token_basis"] = UNAVAILABLE
    row["tokens_fresh"] = None


def _apply_usage(row: dict[str, Any], usage: dict[str, Any]) -> int | None:
    for field in TOKEN_FIELDS:
        row[field] = usage.get(field)
    row["token_basis"] = usage.get("token_basis") or UNAVAILABLE
    fresh = _fresh_from_usage(usage)
    row["tokens_fresh"] = fresh
    return fresh


def backfill_file(results_path: Path, transcripts_dir: Path, write: bool = False) -> dict[str, Any]:
    original_stat = results_path.stat()
    rows = _read_jsonl(results_path)
    adapters = {name: _load_adapter(name) for name in PARSER_HARNESSES}

    lane_stats: dict[str, dict[str, Any]] = defaultdict(_new_lane_stats)
    basis_counts: dict[str, Counter] = defaultdict(Counter)
    pi_mismatches: list[dict[str, Any]] = []
    unavailable_rows: list[dict[str, Any]] = []

    for row in rows:
        harness = str(row.get("harness") or "")
        lane = lane_stats[harness]
        lane["rows"] += 1
        old_tokens = _int_or_none(row.get("tokens"))
        fresh: int | None = None
        basis = row.get("token_basis")

        if harness == GROKBUILD:
            fresh = _fresh_from_usage(row)
            row["tokens_fresh"] = fresh
            if not row.get("token_basis"):
                row["token_basis"] = VENDOR_SPLIT if fresh is not None else UNAVAILABLE
            basis = row.get("token_basis")
            if fresh is None and basis in (UNAVAILABLE, SCALAR_EXACT) and old_tokens is not None:
                row["tokens_fresh"] = old_tokens
                row["token_basis"] = SCALAR_EXACT
                basis = SCALAR_EXACT
                fresh = old_tokens
                lane["scalar_adopted"] += 1
            elif fresh is None and basis in (UNAVAILABLE, SCALAR_EXACT):
                row["token_basis"] = UNAVAILABLE
                basis = UNAVAILABLE
                lane["unavailable"] += 1
                lane["unavailable_reasons"]["native_split_missing"] += 1
            lane["updated"] += 1
            _add_ratio(lane, old_tokens, fresh)
            basis_counts[harness][basis] += 1
            continue

        if harness not in PARSER_HARNESSES:
            _apply_unavailable(row)
            basis = UNAVAILABLE
            lane["updated"] += 1
            lane["unavailable"] += 1
            lane["unavailable_reasons"]["unsupported_harness"] += 1
            unavailable_rows.append({"run_id": row.get("run_id"), "reason": "unsupported_harness"})
            basis_counts[harness][basis] += 1
            continue

        tpath = transcript_path(row, results_path, transcripts_dir)
        raw = ""
        reason: str | None = None
        if tpath is None or not tpath.exists():
            reason = "missing_transcript"
        else:
            try:
                raw = tpath.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                reason = f"read_error:{type(exc).__name__}"
        if reason is None:
            usage, reason = _parse_usage(adapters[harness], harness, raw)
        else:
            usage = _empty_usage()

        if reason is not None:
            _apply_unavailable(row)
            basis = UNAVAILABLE
            if harness in SCALAR_EXACT_HARNESSES and old_tokens is not None:
                row["tokens_fresh"] = old_tokens
                row["token_basis"] = SCALAR_EXACT
                basis = SCALAR_EXACT
                fresh = old_tokens
                lane["scalar_adopted"] += 1
            else:
                lane["unavailable"] += 1
                lane["unavailable_reasons"][reason] += 1
                unavailable_rows.append({"run_id": row.get("run_id"), "reason": reason, "transcript": str(tpath) if tpath else None})
        else:
            fresh = _apply_usage(row, usage)
            basis = row.get("token_basis")
            _add_ratio(lane, old_tokens, fresh)
            if harness == "pi" and old_tokens is not None and fresh != old_tokens:
                pi_mismatches.append({
                    "run_id": row.get("run_id"),
                    "old_tokens": old_tokens,
                    "tokens_fresh": fresh,
                    "transcript": str(tpath),
                })
            if harness == "claude":
                recon = _reconcile_from_usage(usage)
                if old_tokens is not None and recon is not None:
                    lane["claude_reconcile_rows"] += 1
                    diff = recon - old_tokens
                    if diff == 0:
                        lane["claude_reconcile_exact"] += 1
                    else:
                        lane["claude_reconcile_diffs"].append({
                            "run_id": row.get("run_id"),
                            "old_tokens": old_tokens,
                            "recomputed_input_cachewrite_output": recon,
                            "diff": diff,
                        })
        lane["updated"] += 1
        basis_counts[harness][basis] += 1

    if pi_mismatches:
        raise BackfillError("pi exact-match self-test failed: " + json.dumps(pi_mismatches, indent=2))

    backup = None
    backup_created = False
    if write:
        backup, backup_created = _write_jsonl(results_path, rows, original_stat)

    lanes_out: dict[str, Any] = {}
    for harness, stats in sorted(lane_stats.items()):
        lanes_out[harness] = {
            "rows": stats["rows"],
            "updated": stats["updated"],
            "unavailable": stats["unavailable"],
            "scalar_adopted": stats["scalar_adopted"],
            "basis": dict(stats["basis"] or basis_counts[harness]),
            "unavailable_reasons": dict(stats["unavailable_reasons"]),
            "comparable_rows": stats["comparable_rows"],
            "old_sum": stats["old_sum"],
            "tokens_fresh_sum": stats["fresh_sum"],
            "median_fresh_over_old": _median(stats["fresh_over_old"]),
        }
        if harness == "claude":
            lanes_out[harness]["claude_reconcile_rows"] = stats["claude_reconcile_rows"]
            lanes_out[harness]["claude_reconcile_exact"] = stats["claude_reconcile_exact"]
            lanes_out[harness]["claude_reconcile_diffs"] = stats["claude_reconcile_diffs"]

    return {
        "file": str(results_path),
        "rows": len(rows),
        "write": write,
        "backup": str(backup) if backup else str(_backup_path(results_path)),
        "backup_created": backup_created,
        "pi_exact_match_self_test": {"mismatches": 0},
        "lanes": lanes_out,
        "unavailable_rows": unavailable_rows,
    }


def _print_human(summaries: list[dict[str, Any]]) -> None:
    for summary in summaries:
        print(f"\n== {summary['file']} ==")
        print(f"rows={summary['rows']} write={summary['write']} backup={summary['backup']} created={summary['backup_created']}")
        print("pi exact-match self-test: PASS (0 mismatches)")
        print("harness\trows\tunavailable\told_tokens\ttokens_fresh\tmedian_fresh/old\tbasis_counts")
        for harness, lane in summary["lanes"].items():
            median = lane["median_fresh_over_old"]
            median_s = "n/a" if median is None else f"{median:.6g}"
            print(
                f"{harness}\t{lane['rows']}\t{lane['unavailable']}\t{lane['old_sum']}\t"
                f"{lane['tokens_fresh_sum']}\t{median_s}\t{lane['basis']}"
            )
            if lane.get("unavailable_reasons"):
                print(f"  unavailable_reasons={lane['unavailable_reasons']}")
            if harness == "claude":
                print(
                    "  claude old vs input+cache_write+output: "
                    f"exact={lane['claude_reconcile_exact']}/{lane['claude_reconcile_rows']} "
                    f"diffs={len(lane['claude_reconcile_diffs'])}"
                )
                for diff in lane["claude_reconcile_diffs"][:20]:
                    print(f"    diff {diff}")
                if len(lane["claude_reconcile_diffs"]) > 20:
                    print(f"    ... {len(lane['claude_reconcile_diffs']) - 20} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", help="results JSONL file(s) to backfill")
    parser.add_argument("--transcripts-dir", required=True, help="transcripts root or dataset transcript directory")
    parser.add_argument("--write", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--json", action="store_true", help="emit JSON summary instead of a human table")
    args = parser.parse_args(argv)

    transcripts_dir = Path(args.transcripts_dir)
    summaries = []
    try:
        for raw_path in args.results:
            summaries.append(backfill_file(Path(raw_path), transcripts_dir, write=args.write))
    except BackfillError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summaries, indent=2, sort_keys=True))
    else:
        _print_human(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
