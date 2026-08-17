#!/usr/bin/env python3
"""Run one private verifier-backed task through official Codex Computer Use."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
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


def _tree_sha256(root: Path) -> str:
    """Hash one code tree without following links or depending on mtimes."""
    root = root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise SmokeError(f"code tree is unavailable: {root}")
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            target = os.readlink(path).encode("utf-8")
            if not path.resolve().is_relative_to(root):
                raise SmokeError(f"code tree link escapes its root: {path}")
            digest.update(b"L\0" + relative + b"\0" + target + b"\0")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise SmokeError(f"code tree contains an unsupported entry: {path}")
        digest.update(b"F\0" + relative + b"\0" + bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _evidence_files(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json" or path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise SmokeError(f"evidence contains an unsupported entry: {relative}")
        files.append({
            "path": relative,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        })
    return files


def seal_evidence_bundle(root: Path) -> str:
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        raise SmokeError("official evidence manifest already exists")
    manifest = {
        "schema_version": "openbench.official-codex-evidence-manifest.v1",
        "files": _evidence_files(root),
    }
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def verify_evidence_bundle(root: Path) -> str:
    manifest_path = root / "manifest.json"
    try:
        encoded = manifest_path.read_bytes()
        manifest = json.loads(encoded)
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"official evidence manifest is unavailable: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or manifest.get("schema_version")
        != "openbench.official-codex-evidence-manifest.v1"
        or not isinstance(manifest.get("files"), list)
        or manifest["files"] != _evidence_files(root)
    ):
        raise SmokeError("official evidence bundle does not match its manifest")
    return hashlib.sha256(encoded).hexdigest()


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


def _run_cub(request: Path, command: str, trial_index: int) -> int:
    try:
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeError(f"fixture {command} failed: {exc}") from exc
    allowed = (0, 1) if command == "verify" else (0,)
    if completed.returncode not in allowed:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise SmokeError(f"fixture {command} failed: {detail}")
    return completed.returncode


def _owned_fixture_pid(run_root: Path, trial_index: int) -> int:
    state_path = (
        run_root
        / f"runtime/{TASK}/trial{trial_index}/{ARM}/processes.json"
    )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        processes = state["processes"]
        pid = processes[0]["pid"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise SmokeError("fixture process identity is unavailable") from exc
    if len(processes) != 1 or isinstance(pid, bool) or not isinstance(pid, int):
        raise SmokeError("fixture process identity is invalid")
    return pid


def _assert_single_fixture_process(expected_pid: int) -> None:
    completed = subprocess.run(
        ["pgrep", "-x", "ComputerUseFixture"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    try:
        observed = [int(value) for value in completed.stdout.split()]
    except ValueError as exc:
        raise SmokeError("fixture process inventory is malformed") from exc
    if completed.returncode != 0 or observed != [expected_pid]:
        raise SmokeError(
            f"fixture process isolation failed: expected [{expected_pid}], "
            f"observed {observed}"
        )


def _require_file(path: Path, label: str, *, executable: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise SmokeError(f"{label} is unavailable: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise SmokeError(f"{label} is not executable: {resolved}")
    return resolved


def _bundle_identity(app: Path, label: str) -> dict[str, Any]:
    plist_path = _require_file(app / "Contents/Info.plist", f"{label} Info.plist")
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SmokeError(f"cannot read {label} identity: {exc}") from exc
    executable_name = info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise SmokeError(f"{label} has no executable identity")
    executable = _require_file(
        app / "Contents/MacOS" / executable_name,
        f"{label} executable",
        executable=True,
    )
    return {
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
        "executable_sha256": _sha256(executable),
    }


def _service_runtime_identity(
    socket: Path,
    expected_executable: Path,
    *,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    owners = subprocess.run(
        ["lsof", "-t", "--", str(socket)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    try:
        pids = sorted({int(value) for value in owners.stdout.split()})
    except ValueError as exc:
        raise SmokeError("official Computer Use socket owner is malformed") from exc
    if allow_missing and owners.returncode == 1 and not pids:
        return None
    if owners.returncode != 0 or len(pids) != 1:
        raise SmokeError(
            f"official Computer Use socket must have one owner, observed {pids}"
        )

    pid = pids[0]
    process = subprocess.run(
        ["ps", "-p", str(pid), "-o", "comm="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    observed_text = process.stdout.strip()
    observed = Path(observed_text).resolve() if observed_text else None
    expected = expected_executable.resolve()
    if process.returncode != 0 or observed != expected:
        raise SmokeError(
            f"socket owner {pid} is not running the expected service executable"
        )
    return {
        "pid": pid,
        "executable_path": str(observed),
        "executable_sha256": _sha256(observed),
    }


def _monitor_service(
    socket: Path,
    executable: Path,
    initial: dict[str, Any],
) -> tuple[threading.Event, threading.Thread, list[dict[str, Any]], list[SmokeError]]:
    stopped = threading.Event()
    observed = [initial]
    errors: list[SmokeError] = []

    def sample() -> None:
        while not stopped.wait(0.25):
            try:
                identity = _service_runtime_identity(socket, executable)
                observed.append(identity)
            except SmokeError as exc:
                errors.append(exc)
                return

    thread = threading.Thread(target=sample, name="computer-use-service-monitor")
    thread.start()
    return stopped, thread, observed, errors


def _start_service(service_app: Path, socket: Path) -> dict[str, Any]:
    executable = _require_file(
        service_app / "Contents/MacOS/SkyComputerUseService",
        "official Computer Use service",
        executable=True,
    )
    if socket.exists() and not socket.is_symlink():
        identity = _service_runtime_identity(socket, executable)
        identity["started_by_benchmark"] = False
        return identity
    if socket.is_symlink():
        raise SmokeError(f"official Computer Use service socket is a symlink: {socket}")

    completed = subprocess.run(
        ["open", "-n", str(service_app)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeError(
            "cannot launch official Computer Use service: "
            + (completed.stderr or completed.stdout).strip()[-1000:]
        )
    deadline = time.monotonic() + 10
    identity_error: SmokeError | None = None
    while time.monotonic() < deadline:
        if (
            socket.exists()
            and not socket.is_symlink()
            and stat.S_ISSOCK(socket.stat().st_mode)
        ):
            try:
                identity = _service_runtime_identity(socket, executable)
                identity["started_by_benchmark"] = True
                return identity
            except SmokeError as exc:
                identity_error = exc
        time.sleep(0.1)
    if identity_error is not None:
        raise identity_error
    raise SmokeError(
        f"official Computer Use service did not create its socket: {socket} "
        f"({executable})"
    )


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
    service_app = args.service_app.expanduser().resolve()
    if service_app.is_symlink() or not service_app.is_dir():
        raise SmokeError(f"Computer Use service app is unavailable: {service_app}")
    codex_identity = _bundle_identity(codex_app, "Codex app")
    service_identity = _bundle_identity(service_app, "Computer Use service app")
    _require_file(node_modules / "@oai/sky/package.json", "@oai/sky package")
    node_modules_sha256 = _tree_sha256(node_modules)
    service_runtime = _start_service(service_app, socket)
    if service_runtime["executable_sha256"] != service_identity["executable_sha256"]:
        raise SmokeError("live Computer Use service binary does not match its bundle")

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
            fixture_pid = _owned_fixture_pid(run_root, args.trial_index)
            _assert_single_fixture_process(fixture_pid)
            adapter = _load_adapter()
            instruction = (
                TASK_ROOT / TASK / "instruction-official-codex.md"
            ).read_text(encoding="utf-8")
            if instruction.count("{app_path}") != 1:
                raise SmokeError("official instruction must contain one app path marker")
            fixture_app = run_root / "apps/ComputerUseFixture.app"
            instruction = instruction.replace("{app_path}", str(fixture_app))
            attempt = stage / "agent"
            attempt.mkdir()
            service_executable = service_app / "Contents/MacOS/SkyComputerUseService"
            stopped, monitor, service_observations, monitor_errors = _monitor_service(
                socket, service_executable, service_runtime
            )
            try:
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
                        "OPENBENCH_NATIVE_CODEX_APP_PATH": str(fixture_app),
                        "OPENBENCH_NATIVE_TRIAL_ID": (
                            f"cub-v0-official-codex-{TASK}-trial{args.trial_index}"
                        ),
                    },
                )
            finally:
                stopped.set()
                monitor.join(timeout=5)
            if monitor.is_alive():
                raise SmokeError("Computer Use service monitor did not stop")
            if monitor_errors:
                raise monitor_errors[0]
            if len(service_observations) < 2:
                raise SmokeError("Computer Use service was not observed during the trial")
            expected_runtime = {
                key: service_runtime[key]
                for key in ("pid", "executable_path", "executable_sha256")
            }
            if any(
                {
                    key: identity[key]
                    for key in ("pid", "executable_path", "executable_sha256")
                } != expected_runtime
                for identity in service_observations
            ):
                raise SmokeError("Computer Use service implementation changed during the trial")
            _assert_single_fixture_process(fixture_pid)
            events = attempt / "codex-events.jsonl"
            if not events.is_file():
                raise SmokeError("Codex adapter did not retain its event transcript")
            telemetry = summarize_events(events)
            if adapter_result.get("completed") is True:
                verifier_exit = _run_cub(request_path, "verify", args.trial_index)
            verdict = workspace / "runner/verdict.json"
            if verdict.is_file():
                shutil.copyfile(verdict, stage / "verdict.json")
            final_state = workspace / "artifacts/fixture-state.json"
            if final_state.is_file():
                shutil.copyfile(final_state, stage / "fixture-state.json")
            trajectory = workspace / "trajectory.json"
            if trajectory.is_file():
                shutil.copyfile(trajectory, stage / "trajectory.json")
            if _tree_sha256(node_modules) != node_modules_sha256:
                raise SmokeError("official Codex node modules changed during the trial")
            result = {
                "schema_version": "openbench.official-codex-computer-use-smoke.v1",
                "task": TASK,
                "trial_index": args.trial_index,
                "passed": bool(adapter_result.get("completed")) and verifier_exit == 0,
                "agent_completed": adapter_result.get("completed") is True,
                "verifier_exit": verifier_exit,
                "wall_time_s": None,
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
                "node_modules_sha256": node_modules_sha256,
                "codex_app": codex_identity,
                "computer_use_service": service_identity,
                "computer_use_service_runtime": service_runtime,
                "computer_use_service_observations": {
                    "samples": len(service_observations),
                    "pids": sorted({identity["pid"] for identity in service_observations}),
                },
                "telemetry": telemetry,
            }
        except Exception as exc:
            if stage.exists() and not output.exists():
                (stage / "failure.json").write_text(
                    json.dumps(
                        {
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "schema_version": (
                                "openbench.official-codex-computer-use-smoke-failure.v1"
                            ),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(stage, output)
            raise
        finally:
            try:
                _run_cub(request_path, "reset", args.trial_index)
            except SmokeError as exc:
                failure_root = stage if stage.exists() else output
                if failure_root.exists():
                    result_path = failure_root / "result.json"
                    if result_path.exists():
                        result_path.unlink()
                    (failure_root / "failure.json").write_text(
                        json.dumps(
                            {
                                "error": str(exc),
                                "error_type": "cleanup_failure",
                                "schema_version": (
                                    "openbench.official-codex-computer-use-smoke-failure.v1"
                                ),
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    if stage.exists() and not output.exists():
                        os.replace(stage, output)
                raise
        result["wall_time_s"] = time.time() - started
        (stage / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        seal_evidence_bundle(stage)
        os.replace(stage, output)
        return result


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
    parser.add_argument(
        "--service-app",
        type=Path,
        default=Path("~/.codex/computer-use/Codex Computer Use.app"),
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
