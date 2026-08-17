#!/usr/bin/env python3
"""Run a private matched Computer Use OSS versus official Codex comparison."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

import cub_v0 as cub
import official_codex_smoke as official
import scoped_agent_ab as scoped
from obench.native_report import summarize_native_mcp_bundle


TASK = "basic-controls"
OSS_ARM = cub.COMPUTER_USE_OSS_ARM
OFFICIAL_ARM = cub.OFFICIAL_CODEX_ARM


class ComparisonError(RuntimeError):
    pass


def _require_official_terminal(
    result: Mapping[str, Any], returncode: int, block: int
) -> None:
    verifier_exit = result.get("verifier_exit")
    passed = result.get("passed")
    if (
        result.get("agent_completed") is not True
        or isinstance(verifier_exit, bool)
        or verifier_exit not in (0, 1)
    ):
        raise ComparisonError(
            f"official Codex trial {block} has no terminal verifier verdict"
        )
    if not isinstance(passed, bool) or passed != (verifier_exit == 0):
        raise ComparisonError(
            f"official Codex trial {block} has inconsistent verifier evidence"
        )
    expected_returncode = 0 if verifier_exit == 0 else 1
    if returncode != expected_returncode:
        raise ComparisonError(f"official Codex trial {block} was infrastructure-invalid")


def _require_oss_terminal(
    result: Mapping[str, Any], returncode: int, block: int
) -> None:
    checker_exit = result.get("checker_exit")
    score = result.get("score")
    if returncode != 0:
        raise ComparisonError(f"Computer Use OSS trial {block} was infrastructure-invalid")
    if (
        result.get("completed") is not True
        or isinstance(checker_exit, bool)
        or checker_exit not in (0, 1)
    ):
        raise ComparisonError(
            f"Computer Use OSS trial {block} has no terminal checker verdict"
        )
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or ((checker_exit == 0) != (float(score) == 1.0))
    ):
        raise ComparisonError(
            f"Computer Use OSS trial {block} has inconsistent checker evidence"
        )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_last_jsonl(path: Path) -> dict[str, Any]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        value = json.loads(lines[-1])
    except (OSError, IndexError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"cannot load result row {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComparisonError(f"result row is not an object: {path}")
    return value


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def values(name: str) -> list[float]:
        return [
            float(row[name])
            for row in rows
            if isinstance(row.get(name), (int, float))
            and not isinstance(row.get(name), bool)
        ]

    metrics = {}
    for name in (
        "wall_time_s",
        "tokens_fresh",
        "tokens_cache_read",
        "tokens_output",
        "computer_use_calls",
        "computer_use_execution_ms",
        "model_visible_tool_bytes",
        "tool_response_bytes",
    ):
        observed = values(name)
        metrics[name] = {
            "n": len(observed),
            "median": _percentile(observed, 0.5),
            "p95": _percentile(observed, 0.95),
        }
    return {
        "n": len(rows),
        "successes": sum(row.get("success") is True for row in rows),
        "metrics": metrics,
    }


def _oss_row(result: Mapping[str, Any], block: int) -> dict[str, Any]:
    provenance = result.get("candidate_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    return {
        "arm": "computer-use-oss",
        "block": block,
        "success": result.get("score") == 1.0 and result.get("checker_exit") == 0,
        "wall_time_s": result.get("wall_time_s"),
        "tokens_fresh": result.get("tokens_fresh"),
        "tokens_cache_read": result.get("tokens_cache_read"),
        "tokens_output": result.get("tokens_output"),
        "token_basis": result.get("token_basis"),
        "usage_evidence_grade": result.get("usage_evidence_grade"),
        "computer_use_calls": provenance.get("mcp_event_count"),
        "computer_use_execution_ms": None,
        "model_visible_tool_bytes": None,
        "tool_response_bytes": None,
        "result_path": str(result.get("_result_path", "")),
    }


def _official_row(result: Mapping[str, Any], block: int, result_path: Path) -> dict[str, Any]:
    telemetry = result.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    semantic_calls = sum(
        len(call.get("semantic_tools", []))
        for call in telemetry.get("calls", [])
        if isinstance(call, dict) and isinstance(call.get("semantic_tools"), list)
    )
    usage = result.get("token_usage")
    if not isinstance(usage, dict):
        usage = {}
    official_identity = {
        "plugin": result.get("plugin"),
        "node_repl_sha256": result.get("node_repl_sha256"),
        "node_modules_sha256": result.get("node_modules_sha256"),
        "codex_app": result.get("codex_app"),
        "computer_use_service": result.get("computer_use_service"),
        "computer_use_service_runtime": {
            key: result.get("computer_use_service_runtime", {}).get(key)
            for key in ("executable_path", "executable_sha256")
        } if isinstance(result.get("computer_use_service_runtime"), dict) else None,
    }
    return {
        "arm": "codex-computer-use",
        "block": block,
        "success": result.get("passed") is True and result.get("verifier_exit") == 0,
        "wall_time_s": result.get("wall_time_s"),
        "tokens_fresh": result.get("tokens"),
        "tokens_cache_read": usage.get("tokens_cache_read"),
        "tokens_output": usage.get("tokens_output"),
        "token_basis": usage.get("token_basis"),
        "usage_evidence_grade": "agent_reported",
        "computer_use_calls": semantic_calls,
        "computer_use_transport_calls": telemetry.get("call_count"),
        "computer_use_execution_ms": telemetry.get("total_execution_ms"),
        "model_visible_tool_bytes": telemetry.get("total_model_visible_text_bytes"),
        "model_visible_tool_bytes_basis": "direct_text_content_bytes",
        "model_visible_tool_measurement_count": telemetry.get("call_count"),
        "tool_response_bytes": telemetry.get("total_response_bytes"),
        "official_identity": official_identity,
        "result_path": str(result_path),
    }


def _run_official(args: argparse.Namespace, block: int, output: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("official_codex_smoke.py")),
        "--request", str(args.request),
        "--output", str(output),
        "--plugin-dir", str(args.plugin_dir),
        "--codex-app", str(args.codex_app),
        "--service-app", str(args.service_app),
        "--service-socket", str(args.service_socket),
        "--model", args.model,
        "--timeout", str(args.timeout),
        "--trial-index", str(block),
    ]
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
    result_path = output / "result.json"
    if not result_path.is_file():
        raise ComparisonError(f"official Codex trial {block} failed: {output}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    manifest_sha256 = official.verify_evidence_bundle(output)
    _require_official_terminal(result, completed.returncode, block)
    row = _official_row(result, block, result_path)
    row["official_bundle_manifest_sha256"] = manifest_sha256
    return row


def _run_oss(
    *,
    config: Path,
    results: Path,
    runtime_app: Path,
    staged_app: Path,
    expected_binary: str,
    block: int,
) -> dict[str, Any]:
    scoped._stop_daemon()
    scoped._install(staged_app, runtime_app)
    identity = cub._bundle_info(runtime_app)
    if identity["binary_sha256"] != expected_binary:
        raise ComparisonError(f"OSS runtime binary mismatch before block {block}")
    scoped._start_exact_daemon(identity)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "obench", "native", "run", str(config)],
            stdin=subprocess.DEVNULL,
            check=False,
        )
    finally:
        scoped._stop_daemon()
    if not results.is_file():
        raise ComparisonError(f"Computer Use OSS trial {block} produced no result row")
    result = _read_last_jsonl(results)
    _require_oss_terminal(result, completed.returncode, block)
    result["_result_path"] = str(results)
    row = _oss_row(result, block)
    mcp = summarize_native_mcp_bundle(results.parent / "bundle")
    if mcp["call_count"] != row["computer_use_calls"]:
        raise ComparisonError(f"Computer Use OSS trial {block} MCP count mismatch")
    row.update({
        "computer_use_execution_ms": mcp["call_duration_ms"],
        "model_visible_tool_bytes": mcp["context_bytes"],
        "model_visible_tool_measurement_count": mcp["context_measurement_count"],
        "tool_response_bytes": mcp["response_bytes"],
        "model_visible_tool_bytes_basis": "daemon_reported_context_bytes",
    })
    return row


def _configs(
    *,
    request_path: Path,
    request: Mapping[str, Any],
    runtime_app: Path,
    revision: str,
    repetitions: int,
    experiment_id: str,
    timeout_s: int,
    model_name: str,
) -> tuple[list[tuple[Path, Path]], dict[str, Any]]:
    root, repo, _installed = cub._request_paths(request)
    signing_identity = request.get("source_signing_identity")
    if not isinstance(signing_identity, str) or not signing_identity or signing_identity == "-":
        raise ComparisonError("source_signing_identity must be configured")
    staged_app, build = scoped._build_exact_app(
        root=root,
        repo=repo,
        arm="scoped",
        revision=revision,
        signing_identity=signing_identity,
    )
    scoped._install(staged_app, runtime_app)
    identity = cub._bundle_info(runtime_app)
    identity["source_revision"] = revision
    host = cub._host_environment()
    app = cub._bundle_info(cub._task_app_path(root, TASK))
    config_root = cub.descendant(root, f"configs/{experiment_id}")
    outputs: dict[Path, bytes] = {}
    configs = []
    for block in range(1, repetitions + 1):
        config = config_root / "cells" / TASK / f"trial{block}-{OSS_ARM}.toml"
        text = cub._config_text(
            request_path=request_path,
            request=request,
            arm=OSS_ARM,
            task=TASK,
            trial_index=block,
            trial_id=f"cub-v0-{experiment_id}-{OSS_ARM}-trial{block}",
            mcp=identity,
            app=app,
            host=host,
            mode=experiment_id,
            matrix=None,
            instruction_path=cub.ROOT / TASK / "instruction.md",
            locked_state_response_mode=None,
            require_foreground_full_agent_phase=False,
            timeout_s=timeout_s,
            model_name=model_name,
        )
        outputs[config] = text.encode("utf-8")
        _output, results = cub._result_paths(root, experiment_id, OSS_ARM, TASK, block)
        configs.append((config, results))
    cub._write_immutable_outputs(outputs)
    return configs, {
        "staged_app": staged_app,
        "binary_sha256": identity["binary_sha256"],
        "build": build,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    request = cub._load_request(args.request)
    root, _repo, configured_runtime = cub._request_paths(request)
    runtime_app = (args.runtime_app or configured_runtime).expanduser().resolve()
    if not runtime_app.is_dir() or runtime_app.is_symlink():
        raise ComparisonError(f"authorized OSS runtime app is unavailable: {runtime_app}")
    prompt = (cub.ROOT / TASK / "instruction.md").read_text(encoding="utf-8")
    forbidden_prompt_markers = (
        "include_state", "include_screenshot", "element_id", "mcp__", "exactly these calls",
    )
    if any(marker in prompt for marker in forbidden_prompt_markers):
        raise ComparisonError("canonical task prompt contains tool-strategy instructions")

    comparison_root = cub.descendant(root, f"results/{args.experiment_id}")
    progress_path = comparison_root / "progress.json"
    report_path = comparison_root / "comparison.json"
    if comparison_root.exists():
        raise ComparisonError(f"comparison output already exists: {comparison_root}")
    comparison_root.mkdir(parents=True)

    backup = runtime_app.with_name(runtime_app.name + ".official-vs-oss-backup")
    if backup.exists():
        raise ComparisonError(f"stale runtime backup requires inspection: {backup}")
    scoped._stop_daemon()
    scoped._copy_bundle(runtime_app, backup)
    primary_error: BaseException | None = None
    rows: list[dict[str, Any]] = []
    try:
        configs, oss = _configs(
            request_path=args.request,
            request=request,
            runtime_app=runtime_app,
            revision=args.oss_revision,
            repetitions=args.repetitions,
            experiment_id=args.experiment_id,
            timeout_s=args.timeout,
            model_name=args.model,
        )
        staged_app = Path(oss["staged_app"])
        for block, (config, results) in enumerate(configs, start=1):
            order = ("computer-use-oss", "codex-computer-use") if block % 2 else (
                "codex-computer-use", "computer-use-oss"
            )
            for arm in order:
                print(f"RUN block={block} arm={arm}", flush=True)
                if arm == "computer-use-oss":
                    row = _run_oss(
                        config=config,
                        results=results,
                        runtime_app=runtime_app,
                        staged_app=staged_app,
                        expected_binary=oss["binary_sha256"],
                        block=block,
                    )
                else:
                    scoped._stop_daemon()
                    output = comparison_root / "official-codex" / f"trial{block}"
                    row = _run_official(args, block, output)
                row["position"] = order.index(arm) + 1
                rows.append(row)
                _atomic_json(progress_path, {
                    "schema_version": "openbench.computer-use-backend-progress.v1",
                    "experiment_id": args.experiment_id,
                    "planned_cells": args.repetitions * 2,
                    "completed_cells": len(rows),
                    "rows": rows,
                })
        by_arm = {
            arm: [row for row in rows if row["arm"] == arm]
            for arm in ("computer-use-oss", "codex-computer-use")
        }
        official_identities = {
            json.dumps(row.get("official_identity"), sort_keys=True, separators=(",", ":"))
            for row in by_arm["codex-computer-use"]
        }
        if len(official_identities) != 1:
            raise ComparisonError("official Codex identity changed across comparison cells")
        official_identity = json.loads(next(iter(official_identities)))
        report = {
            "schema_version": "openbench.computer-use-backend-comparison.v1",
            "experiment_id": args.experiment_id,
            "design": "matched_interleaved_forward_reverse",
            "task": "openbench/computer-use-v0-basic-controls",
            "model": args.model,
            "repetitions": args.repetitions,
            "timeout_s": args.timeout,
            "prompts": {
                "computer-use-oss": {
                    "path": str(cub.ROOT / TASK / "instruction.md"),
                    "sha256": scoped._sha256(cub.ROOT / TASK / "instruction.md"),
                },
                "codex-computer-use": {
                    "path": str(cub.ROOT / TASK / "instruction-official-codex.md"),
                    "sha256": scoped._sha256(
                        cub.ROOT / TASK / "instruction-official-codex.md"
                    ),
                },
            },
            "oss_revision": args.oss_revision,
            "oss_binary_sha256": oss["binary_sha256"],
            "official_identity": official_identity,
            "notes": [
                "Pass/fail, wall time, and final state share one checker and fixture.",
                "Both arms receive the same natural task goal and no task-specific tool strategy.",
                "Each backend retains its native tool schema or generic interface skill.",
                "Both arms expose get_app_state, click, set_value, and type_text for this task.",
                "OSS usage is proxy-reconciled; official Codex usage is agent-reported.",
                "Official node_repl transport calls may batch multiple semantic operations.",
                "OSS model-visible bytes are daemon-reported context bytes; official bytes are directly counted text content.",
            ],
            "arms": {arm: _summary(arm_rows) for arm, arm_rows in by_arm.items()},
            "rows": rows,
        }
        _atomic_json(report_path, report)
        return report
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        scoped._restore_runtime_app(runtime_app, backup, primary_error=primary_error)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--plugin-dir", required=True, type=Path)
    parser.add_argument("--oss-revision", required=True)
    parser.add_argument("--runtime-app", type=Path)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--experiment-id", default="official-vs-oss-neutral")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--codex-app", type=Path, default=Path("/Applications/Codex.app"))
    parser.add_argument(
        "--service-app",
        type=Path,
        default=Path("~/.codex/computer-use/Codex Computer Use.app"),
    )
    parser.add_argument(
        "--service-socket",
        type=Path,
        default=Path(
            "~/Library/Group Containers/2DC432GLL2.com.openai.sky.CUAService/IPC/computeruse.sock"
        ),
    )
    args = parser.parse_args(argv)
    args.request = args.request.expanduser().resolve()
    args.plugin_dir = args.plugin_dir.expanduser().resolve()
    args.codex_app = args.codex_app.expanduser().resolve()
    args.service_app = args.service_app.expanduser().resolve()
    args.service_socket = args.service_socket.expanduser().resolve()
    if args.repetitions < 2 or args.repetitions % 2:
        parser.error("repetitions must be a positive even number")
    if args.timeout < 1:
        parser.error("timeout must be positive")
    if len(args.oss_revision) != 40 or any(c not in "0123456789abcdef" for c in args.oss_revision):
        parser.error("oss-revision must be one full lowercase commit SHA")
    try:
        report = run(args)
    except (ComparisonError, scoped.ExperimentError, cub.CubError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
