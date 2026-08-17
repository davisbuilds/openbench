#!/usr/bin/env python3
"""Run one private verifier-backed task through official Codex Computer Use."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from obench.codex_computer_use import summarize_events


TASK_ROOT = Path(__file__).resolve().parents[1]
CUB = Path(__file__).with_name("cub_v0.py")
ADAPTER = REPO_ROOT / "obench/adapters/codex.py"
ARM = "official-codex"
TASK = "basic-controls"


class SmokeError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_adapter():
    spec = importlib.util.spec_from_file_location("official_codex_smoke_adapter", ADAPTER)
    if spec is None or spec.loader is None:
        raise SmokeError("cannot load Codex adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SmokeError(f"cannot load request: {exc}") from exc
    run_root = value.get("run_root")
    if not isinstance(run_root, str) or not run_root.startswith("/"):
        raise SmokeError("request run_root must be an absolute path")
    return value


def _run_cub(request: Path, command: str, trial_index: int) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(CUB),
            "--request",
            str(request),
            command,
            "--arm",
            ARM,
            "--task",
            TASK,
            "--trial-index",
            str(trial_index),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise SmokeError(f"fixture {command} failed: {detail}")


def _require_file(path: Path, label: str, *, executable: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise SmokeError(f"{label} is unavailable: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise SmokeError(f"{label} is not executable: {resolved}")
    return resolved


def run(args: argparse.Namespace) -> dict[str, Any]:
    request_path = args.request.expanduser().resolve()
    request = _request(request_path)
    run_root = Path(request["run_root"]).expanduser().resolve()
    workspace = (
        run_root / f"workspaces/{TASK}/trial{args.trial_index}/{ARM}"
    )
    output = args.output.expanduser().resolve()
    if output.exists():
        raise SmokeError(f"output already exists: {output}")

    codex_app = args.codex_app.expanduser().resolve()
    node_repl = _require_file(
        codex_app / "Contents/Resources/cua_node/bin/node_repl",
        "official Codex node_repl",
        executable=True,
    )
    node_modules = (
        codex_app / "Contents/Resources/cua_node/lib/node_modules"
    ).resolve()
    if node_modules.is_symlink() or not node_modules.is_dir():
        raise SmokeError(f"official Codex node modules are unavailable: {node_modules}")
    plugin_dir = args.plugin_dir.expanduser().resolve()
    manifest = _require_file(
        plugin_dir / ".codex-plugin/plugin.json", "Computer Use plugin manifest"
    )
    skill = _require_file(
        plugin_dir / "skills/computer-use/SKILL.md", "Computer Use skill"
    )
    plugin = json.loads(manifest.read_text(encoding="utf-8"))
    if plugin.get("name") != "computer-use" or not isinstance(plugin.get("version"), str):
        raise SmokeError("Computer Use plugin manifest identity is invalid")
    socket = args.service_socket.expanduser().resolve()
    if socket.is_symlink() or not socket.exists():
        raise SmokeError(f"Computer Use service socket is unavailable: {socket}")

    started = time.time()
    adapter_result: dict[str, Any] | None = None
    verifier_exit: int | None = None
    temp_parent = output.parent
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=temp_parent
    ) as temporary:
        stage = Path(temporary) / output.name
        stage.mkdir()
        try:
            _run_cub(request_path, "reset", args.trial_index)
            _run_cub(request_path, "setup", args.trial_index)
            adapter = _load_adapter()
            instruction = (
                TASK_ROOT / TASK / "instruction-official-codex.md"
            ).read_text(encoding="utf-8")
            attempt = stage / "agent"
            attempt.mkdir()
            adapter_result = adapter.run(
                instruction,
                str(workspace),
                args.model,
                args.timeout,
                env_override={
                    "OPENBENCH_NATIVE_COMPUTER_USE_PROFILE": "official_codex",
                    "OPENBENCH_NATIVE_CODEX_NODE_REPL_COMMAND": str(node_repl),
                    "OPENBENCH_NATIVE_CODEX_NODE_MODULE_DIRS": str(node_modules),
                    "OPENBENCH_NATIVE_CODEX_SKILL_PATH": str(skill),
                    "OPENBENCH_NATIVE_EVIDENCE_DIR": str(attempt),
                    "OPENBENCH_NATIVE_TRIAL_ID": (
                        f"cub-v0-official-codex-{TASK}-trial{args.trial_index}"
                    ),
                },
            )
            events = attempt / "codex-events.jsonl"
            if not events.is_file():
                raise SmokeError("Codex adapter did not retain its event transcript")
            telemetry = summarize_events(events)
            if adapter_result.get("completed") is True:
                try:
                    _run_cub(request_path, "verify", args.trial_index)
                    verifier_exit = 0
                except SmokeError:
                    verifier_exit = 1
            verdict = workspace / "runner/verdict.json"
            if verdict.is_file():
                shutil.copyfile(verdict, stage / "verdict.json")
            final_state = workspace / "artifacts/fixture-state.json"
            if final_state.is_file():
                shutil.copyfile(final_state, stage / "fixture-state.json")
            trajectory = workspace / "trajectory.json"
            if trajectory.is_file():
                shutil.copyfile(trajectory, stage / "trajectory.json")
            result = {
                "schema_version": "openbench.official-codex-computer-use-smoke.v1",
                "task": TASK,
                "trial_index": args.trial_index,
                "passed": bool(adapter_result.get("completed")) and verifier_exit == 0,
                "verifier_exit": verifier_exit,
                "wall_time_s": time.time() - started,
                "model": args.model,
                "tokens": adapter_result.get("tokens"),
                "turns": adapter_result.get("turns"),
                "token_usage": {
                    key: adapter_result.get(key)
                    for key in (
                        "tokens_input_uncached",
                        "tokens_cache_read",
                        "tokens_cache_write",
                        "tokens_output",
                        "tokens_reasoning",
                        "token_basis",
                    )
                },
                "error": adapter_result.get("error"),
                "plugin": {
                    "name": plugin["name"],
                    "version": plugin["version"],
                    "manifest_sha256": _sha256(manifest),
                    "skill_sha256": _sha256(skill),
                },
                "node_repl_sha256": _sha256(node_repl),
                "telemetry": telemetry,
            }
            (stage / "result.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(stage, output)
            return result
        finally:
            try:
                _run_cub(request_path, "reset", args.trial_index)
            except SmokeError:
                if adapter_result is None:
                    raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plugin-dir", required=True, type=Path)
    parser.add_argument("--codex-app", type=Path, default=Path("/Applications/Codex.app"))
    parser.add_argument(
        "--service-socket",
        type=Path,
        default=Path(
            "~/Library/Group Containers/2DC432GLL2.com.openai.sky.CUAService/IPC/computeruse.sock"
        ),
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--trial-index", type=int, default=1)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.trial_index <= 0:
        parser.error("timeout and trial-index must be positive")
    try:
        result = run(args)
    except (SmokeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
