"""Experimental native macOS Computer-Use trial runner.

This runner owns native host orchestration and emits the evidence contract
validated by :mod:`obench.native_trial`.  It deliberately does not synthesize
Harbor jobs, locks, or execution identity.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any, Callable, Iterator, Mapping, Sequence

from .atif import SCHEMA_VERSION as ATIF_SCHEMA_VERSION, assert_valid_trajectory
from .mcp_stdio_collector import CallLedger, collect_stdio, verify_ledger
from .native_macos import (
    AppRequirement,
    LeaseOwner,
    MacOSFocusMonitor,
    PhaseName,
    PhaseSpec,
    PreflightResult,
    PreflightSpec,
    SubprocessPhaseRunner,
    WholeRunLease,
    run_preflight,
)
from .native_trial import (
    BUNDLE_SCHEMA_VERSION,
    LEDGER_SCHEMA_VERSION,
    NATIVE_SIDECAR_SCHEMA_VERSION,
    TASK_SIDECAR_SCHEMA_VERSION,
    import_native_trial,
    load_native_trial,
)
from .paths import default_adapters_dir
from .run import (
    _temporary_environ,
    apply_proxy_ledger,
    load_adapter,
    probe_version,
    proxy_supported_for_cell,
    read_proxy_ledger,
)


CONFIG_SCHEMA_VERSION = "openbench.native-run.v0"
DEFAULT_LEASE_PATH = "~/.openbench/native-macos.lock"


class NativeRunError(RuntimeError):
    """Native trial configuration, execution, or sealing failed closed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(value) + b"\n"
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(_canonical_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _safe_relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeRunError(f"{field} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise NativeRunError(f"{field} must be a normalized relative POSIX path")
    return value


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NativeRunError(f"{field} must be a non-empty trimmed string")
    return value


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise NativeRunError(f"{field} must be positive")
    return float(value)


def _command(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise NativeRunError(f"{field} must be a non-empty array of strings")
    return tuple(value)


@dataclass(frozen=True)
class CommandConfig:
    argv: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class ArtifactConfig:
    source: str
    path: str
    media_type: str


@dataclass(frozen=True)
class NativeRunConfig:
    source_path: Path
    trial_id: str
    output_dir: Path
    results_path: Path
    lease_path: Path
    workspace: Path
    task_id: str
    instruction_path: Path
    app_bundle_id: str
    harness_name: str
    harness_version: str
    harness_version_source: str
    adapter_path: Path | None
    adapters_dir: Path
    model_name: str
    model_provider: str
    model_revision: str
    mcp_name: str
    mcp_version: str
    mcp_command: tuple[str, ...]
    mcp_client_command_env: str
    environment: Mapping[str, Any]
    timeout_s: float
    max_retries: int
    setup: CommandConfig
    verifier: CommandConfig
    reset: CommandConfig
    atif_path: str
    verdict_path: str
    artifacts: tuple[ArtifactConfig, ...]
    proxy_required: bool


def load_config(path: str | os.PathLike[str]) -> NativeRunConfig:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise NativeRunError(f"cannot load native config {source}: {exc}") from exc
    if data.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise NativeRunError(f"schema_version must be {CONFIG_SCHEMA_VERSION!r}")
    root = source.parent

    def table(name: str) -> dict[str, Any]:
        value = data.get(name)
        if not isinstance(value, dict):
            raise NativeRunError(f"[{name}] is required")
        return value

    task, harness, model, mcp = (table(name) for name in ("task", "harness", "model", "mcp"))
    environment, budget, phases = (table(name) for name in ("environment", "budget", "phases"))
    app = environment.get("app")
    if not isinstance(app, dict):
        raise NativeRunError("[environment.app] is required")

    def resolve(value: Any, field: str) -> Path:
        text = _required_string(value, field)
        candidate = Path(text).expanduser()
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    def phase(name: str) -> CommandConfig:
        value = phases.get(name)
        if not isinstance(value, dict):
            raise NativeRunError(f"[phases.{name}] is required")
        return CommandConfig(
            _command(value.get("command"), f"phases.{name}.command"),
            _positive_number(value.get("timeout_s"), f"phases.{name}.timeout_s"),
        )

    raw_artifacts = data.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise NativeRunError("at least one [[artifacts]] entry is required")
    artifacts = []
    for index, item in enumerate(raw_artifacts):
        if not isinstance(item, dict):
            raise NativeRunError(f"artifacts[{index}] must be a table")
        path_value = _safe_relative(item.get("path"), f"artifacts[{index}].path")
        if not path_value.startswith("artifacts/final-state/"):
            raise NativeRunError(f"artifacts[{index}].path must be below artifacts/final-state/")
        media_type = _required_string(item.get("media_type"), f"artifacts[{index}].media_type")
        if media_type.startswith("image/"):
            raise NativeRunError(f"artifacts[{index}] cannot import a raw screenshot")
        artifacts.append(ArtifactConfig(
            source=_safe_relative(item.get("source"), f"artifacts[{index}].source"),
            path=path_value,
            media_type=media_type,
        ))
    if len({item.path for item in artifacts}) != len(artifacts):
        raise NativeRunError("artifact bundle paths must be unique")

    trial_id = _required_string(data.get("trial_id"), "trial_id")
    if re.search(r"(?:^|[-_:])trial[1-9][0-9]*$", trial_id) is None:
        raise NativeRunError("trial_id must end with an explicit positive trialN index")
    max_retries = budget.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise NativeRunError("budget.max_retries must be a non-negative integer")
    proxy = data.get("proxy", {})
    if not isinstance(proxy, dict):
        raise NativeRunError("[proxy] must be a table")
    adapter_path = harness.get("candidate")
    return NativeRunConfig(
        source_path=source,
        trial_id=trial_id,
        output_dir=resolve(data.get("output_dir"), "output_dir"),
        results_path=resolve(data.get("results_path"), "results_path"),
        lease_path=Path(str(data.get("lease_path", DEFAULT_LEASE_PATH))).expanduser().resolve(),
        workspace=resolve(data.get("workspace"), "workspace"),
        task_id=_required_string(task.get("id"), "task.id"),
        instruction_path=resolve(task.get("instruction"), "task.instruction"),
        app_bundle_id=_required_string(app.get("bundle_id"), "environment.app.bundle_id"),
        harness_name=_required_string(harness.get("name"), "harness.name"),
        harness_version=_required_string(harness.get("version"), "harness.version"),
        harness_version_source=_required_string(harness.get("version_source", "native_cli"), "harness.version_source"),
        adapter_path=resolve(adapter_path, "harness.candidate") if adapter_path else None,
        adapters_dir=resolve(harness.get("adapters_dir"), "harness.adapters_dir") if harness.get("adapters_dir") else Path(default_adapters_dir()).resolve(),
        model_name=_required_string(model.get("name"), "model.name"),
        model_provider=_required_string(model.get("provider"), "model.provider"),
        model_revision=_required_string(model.get("revision"), "model.revision"),
        mcp_name=_required_string(mcp.get("name", "computer-use-mcp"), "mcp.name"),
        mcp_version=_required_string(mcp.get("version"), "mcp.version"),
        mcp_command=_command(mcp.get("command"), "mcp.command"),
        mcp_client_command_env=_required_string(mcp.get("client_command_env"), "mcp.client_command_env"),
        environment=environment,
        timeout_s=_positive_number(budget.get("timeout_s"), "budget.timeout_s"),
        max_retries=max_retries,
        setup=phase("setup"),
        verifier=phase("verifier"),
        reset=phase("reset"),
        atif_path=_safe_relative(data.get("atif_path"), "atif_path"),
        verdict_path=_safe_relative(data.get("verdict_path"), "verdict_path"),
        artifacts=tuple(artifacts),
        proxy_required=bool(proxy.get("required", False)),
    )


@dataclass
class NativeRunHooks:
    preflight: Callable[[PreflightSpec], PreflightResult] = run_preflight
    phase_runner_factory: Callable[[], SubprocessPhaseRunner] = SubprocessPhaseRunner
    focus_monitor_factory: Callable[[Sequence[str]], Any] = lambda allowed: MacOSFocusMonitor(allowed)
    adapter_loader: Callable[[NativeRunConfig], Any] | None = None
    version_probe: Callable[[NativeRunConfig, Any], str | None] | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    monotonic: Callable[[], float] = time.monotonic
    proxy_factory: Callable[[Path], Any] | None = None


@dataclass(frozen=True)
class NativeRunOutcome:
    bundle_dir: Path
    results_path: Path
    row: Mapping[str, Any]
    attempts_dir: Path


def _load_adapter(config: NativeRunConfig) -> Any:
    if config.adapter_path is not None:
        from .candidates import load_candidate
        return load_candidate(str(config.adapter_path), str(config.adapters_dir))
    return load_adapter(str(config.adapters_dir), config.harness_name)


def _probe_version(config: NativeRunConfig, adapter: Any) -> str | None:
    candidate = adapter if config.adapter_path is not None else None
    return probe_version(config.harness_name, str(config.adapters_dir), candidate)


def _server_executable(command: Sequence[str]) -> Path:
    first = Path(command[0]).expanduser()
    resolved = first if first.is_absolute() else Path(shutil.which(command[0]) or "")
    if not resolved or not resolved.is_file():
        raise NativeRunError(f"MCP server executable is not a regular file: {command[0]!r}")
    return resolved.resolve()


def _expand_command(command: Sequence[str], config: NativeRunConfig) -> tuple[str, ...]:
    values = {
        "workspace": str(config.workspace),
        "config_dir": str(config.source_path.parent),
    }
    return tuple(
        part.replace("{workspace}", values["workspace"]).replace("{config_dir}", values["config_dir"])
        for part in command
    )


def _phase(config: NativeRunConfig, name: PhaseName, item: CommandConfig) -> PhaseSpec:
    return PhaseSpec(name, _expand_command(item.argv, config), item.timeout_s, cwd=str(config.workspace))


def _empty_mcp_ledger(path: Path, run_id: str, trial_id: str) -> None:
    ledger = CallLedger(path, run_id, trial_id)
    ledger.seal({
        "returncode": 0,
        "integrity_ok": True,
        "malformed_frames": 0,
        "partial_frames": 0,
        "duplicate_request_ids": 0,
        "missing_responses": 0,
        "input_incomplete": False,
    })


def _collector_launcher(path: Path) -> None:
    package_root = Path(__file__).resolve().parent.parent
    script = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(package_root)!r})\n"
        "from obench.native_run import collector_main\n"
        "raise SystemExit(collector_main())\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)


def collector_main() -> int:
    try:
        command = json.loads(os.environ["OPENBENCH_NATIVE_MCP_SERVER_COMMAND"])
        if not isinstance(command, list):
            raise ValueError("server command must be an array")
        result = collect_stdio(
            command,
            ledger_path=os.environ["OPENBENCH_NATIVE_MCP_LEDGER"],
            run_id=os.environ["OPENBENCH_NATIVE_COLLECTOR_RUN_ID"],
            trial_id=os.environ["OPENBENCH_NATIVE_TRIAL_ID"],
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
        )
        return result.returncode
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"native MCP collector configuration error: {exc}", file=sys.stderr)
        return 2


class _LedgerWriter:
    def __init__(self, root: Path, prefix: str, trial_id: str, lock_sha256: str):
        self.root, self.prefix = root, prefix
        self.trial_id, self.lock_sha256 = trial_id, lock_sha256
        self.records: list[dict[str, Any]] = []

    def append(self, kind: str, payload: Mapping[str, Any], timestamp: str) -> None:
        previous = self.records[-1]["record_hash"] if self.records else "0" * 64
        record = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "trial_id": self.trial_id,
            "lock_sha256": self.lock_sha256,
            "sequence": len(self.records) + 1,
            "kind": kind,
            "timestamp": timestamp,
            "payload": dict(payload),
            "previous_hash": previous,
        }
        record["record_hash"] = _canonical_digest(record)
        self.records.append(record)

    def seal(self) -> None:
        directory = self.root / self.prefix
        directory.mkdir(parents=True, exist_ok=True)
        ledger = directory / "ledger.jsonl"
        with ledger.open("xb") as handle:
            for record in self.records:
                handle.write(_canonical_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        count = len(self.records)
        _write_json(directory / "seal.json", {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "trial_id": self.trial_id,
            "lock_sha256": self.lock_sha256,
            "record_count": count,
            "last_sequence": count,
            "root_hash": self.records[-1]["record_hash"] if self.records else "0" * 64,
            "ledger_sha256": _sha256(ledger),
        })


def _copy_atif(config: NativeRunConfig, bundle: Path, started: datetime) -> dict[str, Any]:
    source = config.workspace / config.atif_path
    try:
        trajectory = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeRunError(f"required ATIF trajectory is unavailable: {exc}") from exc
    trajectory["schema_version"] = ATIF_SCHEMA_VERSION
    trajectory["trajectory_id"] = config.trial_id
    trajectory["agent"] = {
        "name": config.harness_name,
        "version": config.harness_version,
        "model_name": config.model_name,
    }
    for step in trajectory.get("steps", []):
        step.setdefault("timestamp", started.isoformat())
        if step.get("source") == "user":
            step["message"] = (
                "[instruction omitted; sha256="
                + _sha256(config.instruction_path)
                + "]"
            )
        if step.get("source") == "agent":
            step.setdefault("model_name", config.model_name)
    assert_valid_trajectory(trajectory)
    _write_json(bundle / "agent/trajectory.json", trajectory)
    return trajectory


def _copy_artifacts(config: NativeRunConfig, bundle: Path) -> list[dict[str, Any]]:
    entries = []
    for item in config.artifacts:
        source = config.workspace / item.source
        if not source.is_file() or source.is_symlink():
            raise NativeRunError(f"required final-state artifact is unavailable: {item.source}")
        destination = bundle / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        entries.append({
            "path": item.path,
            "sha256": _sha256(destination),
            "size": destination.stat().st_size,
            "media_type": item.media_type,
            "classification": "public_evidence",
        })
    return entries


def _final_state_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    return _canonical_digest([
        {"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
        for item in entries
    ])


def _write_manifest(bundle: Path, trial_id: str) -> None:
    files = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        relative = path.relative_to(bundle).as_posix()
        if relative == "manifest.json":
            continue
        files.append({"path": relative, "sha256": _sha256(path), "size": path.stat().st_size})
    _write_json(bundle / "manifest.json", {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "trial_id": trial_id,
        "lock_sha256": _sha256(bundle / "lock.json"),
        "result_sha256": _sha256(bundle / "result.json"),
        "files": files,
    })


def _proxy_context(
    config: NativeRunConfig,
    directory: Path,
    hooks: NativeRunHooks,
    proxy_harness: str,
):
    if not config.proxy_required:
        return None
    if not proxy_supported_for_cell(proxy_harness, config.model_name):
        raise NativeRunError("counting proxy is required but the harness/model route is unsupported")
    if hooks.proxy_factory is not None:
        return hooks.proxy_factory(directory)
    from .proxy import start_in_thread
    server, thread = start_in_thread("127.0.0.1", 0, directory, require_registered_tokens=True)
    return server, thread


@contextmanager
def _managed_proxy(
    config: NativeRunConfig,
    directory: Path,
    hooks: NativeRunHooks,
    token: str,
    proxy_harness: str,
) -> Iterator[dict[str, Any] | None]:
    created = _proxy_context(config, directory, hooks, proxy_harness)
    if created is None:
        yield None
        return
    server, thread = created
    host, port = server.server_address
    server.register_cell(token)
    context = {
        "server": server,
        "thread": thread,
        "ledger_dir": directory,
        "token": token,
        "env": {
            "OPENBENCH_PROXY": "1",
            "OPENBENCH_PROXY_BASE_URL": f"http://{host}:{port}",
            "OPENBENCH_PROXY_CELL_TOKEN": token,
        },
    }
    try:
        yield context
        server.seal_cell(token, timeout_s=5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _proxy_usage(context: Mapping[str, Any] | None) -> list[dict[str, int]]:
    if context is None:
        return []
    rows = read_proxy_ledger(context["ledger_dir"], context["token"])
    if not rows or rows[-1].get("record_type") != "ledger_seal":
        raise NativeRunError("counting proxy ledger is not durably sealed")
    measured: dict[str, Any] = {}
    apply_proxy_ledger(measured, rows[:-1])
    if measured.get("proxy_capture_truncated") or measured.get("token_basis_proxy") != "proxy_measured":
        if measured.get("tokens_proxy_calls") == 0:
            return []
        raise NativeRunError("counting proxy evidence is incomplete")
    return [{
        "input_tokens": int(measured.get("tokens_proxy_input_uncached") or 0) + int(measured.get("tokens_proxy_cache_read") or 0),
        "cached_tokens": int(measured.get("tokens_proxy_cache_read") or 0),
        "output_tokens": int(measured.get("tokens_proxy_output") or 0),
    }]


def _attempt_record(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_json(path, value)


def run_native(config_or_path: NativeRunConfig | str | os.PathLike[str], *, hooks: NativeRunHooks | None = None) -> NativeRunOutcome:
    config = config_or_path if isinstance(config_or_path, NativeRunConfig) else load_config(config_or_path)
    hooks = hooks or NativeRunHooks()
    for required in (config.workspace, config.instruction_path):
        if not required.exists():
            raise NativeRunError(f"required native input does not exist: {required}")
    if config.output_dir.exists():
        raise NativeRunError(f"native output already exists: {config.output_dir}")
    attempts_dir = config.output_dir.with_name(config.output_dir.name + ".attempts")
    if attempts_dir.exists():
        raise NativeRunError(f"native attempts output already exists: {attempts_dir}")

    owner = LeaseOwner.current(config.trial_id)
    with WholeRunLease(config.lease_path, owner=owner):
        adapter = (hooks.adapter_loader or _load_adapter)(config)
        observed_version = (hooks.version_probe or _probe_version)(config, adapter)
        if observed_version != config.harness_version:
            raise NativeRunError(
                f"harness version mismatch: expected {config.harness_version!r}, observed {observed_version!r}"
            )
        mcp_executable = _server_executable(config.mcp_command)
        preflight_spec = PreflightSpec(
            required_apps=(AppRequirement(config.app_bundle_id, str(config.environment["app"]["version"])),),
            computer_use_version=config.mcp_version,
            computer_use_bundle_identifier=config.environment.get("mcp_bundle_id"),
        )
        preflight = hooks.preflight(preflight_spec)
        preflight.require_passed()
        if preflight.health is None:
            if hooks.preflight is run_preflight:
                raise NativeRunError("native preflight omitted source-proven MCP health")
        else:
            health_executable = Path(preflight.health.executable_path).expanduser().resolve()
            if health_executable != mcp_executable:
                raise NativeRunError(
                    "MCP health executable does not match the locked server executable"
                )
        phase_runner = hooks.phase_runner_factory()
        setup_spec = _phase(config, PhaseName.SETUP, config.setup)
        verifier_spec = _phase(config, PhaseName.VERIFIER, config.verifier)
        reset_spec = _phase(config, PhaseName.RESET, config.reset)
        collector_run_id = f"{config.trial_id}-mcp"
        instruction = config.instruction_path.read_text(encoding="utf-8")
        started = hooks.clock()
        start_mono = hooks.monotonic()
        setup_s = agent_s = verifier_s = 0.0
        chosen_mcp: Path | None = None
        proxy_usage: list[dict[str, int]] = []
        focus_events: list[tuple[str, Mapping[str, Any]]] = []
        adapter_result: Mapping[str, Any] | None = None
        verifier_outcome = None
        completed_attempts = 0
        monitor = hooks.focus_monitor_factory((config.app_bundle_id,))
        try:
            monitor.start()
            focus_events.append((started.isoformat(), {
                "state": "target_focused",
                "frontmost_bundle_id": config.app_bundle_id,
                "target_bundle_id": config.app_bundle_id,
            }))
            for attempt in range(1, config.max_retries + 2):
                completed_attempts = attempt
                attempt_root = attempts_dir / f"attempt{attempt}"
                attempt_root.mkdir(parents=True, exist_ok=False)
                setup_outcome = phase_runner.run_phase(setup_spec)
                setup_s += setup_outcome.duration_s
                if not setup_outcome.passed:
                    _attempt_record(attempt_root / "attempt.json", {
                        "attempt": attempt, "phase": "setup", "status": setup_outcome.status.value,
                        "exit_code": setup_outcome.exit_code,
                    })
                    raise NativeRunError(f"setup phase {setup_outcome.status.value}")

                launcher = attempt_root / "computer-use-mcp-collector"
                ledger = attempt_root / "mcp-ledger.jsonl"
                _collector_launcher(launcher)
                env = {
                    config.mcp_client_command_env: str(launcher),
                    "OPENBENCH_NATIVE_MCP_SERVER_COMMAND": json.dumps(list(config.mcp_command)),
                    "OPENBENCH_NATIVE_MCP_LEDGER": str(ledger),
                    "OPENBENCH_NATIVE_COLLECTOR_RUN_ID": collector_run_id,
                    "OPENBENCH_NATIVE_TRIAL_ID": config.trial_id,
                }
                token = f"native-{attempt}"
                proxy_dir = attempt_root / "proxy-raw"
                before = hooks.monotonic()
                proxy_harness = (
                    getattr(adapter, "proxy_adapter", None)
                    or getattr(adapter, "base_adapter", None)
                    or config.harness_name
                )
                with _managed_proxy(
                    config, proxy_dir, hooks, token, proxy_harness
                ) as proxy_context:
                    if proxy_context:
                        env.update(proxy_context["env"])
                    with _temporary_environ(env):
                        adapter_result = adapter.run(
                            instruction, str(config.workspace), config.model_name, int(config.timeout_s)
                        )
                proxy_usage = _proxy_usage(proxy_context)
                if not isinstance(adapter_result, Mapping):
                    raise NativeRunError("adapter returned a non-object result")
                agent_s += max(0.0, hooks.monotonic() - before)
                if not ledger.exists():
                    if adapter_result.get("startup_failure") is True:
                        _empty_mcp_ledger(ledger, collector_run_id, config.trial_id)
                    else:
                        raise NativeRunError("harness did not launch the configured MCP collector")
                verified_mcp = verify_ledger(ledger)
                startup_retry = (
                    adapter_result.get("startup_failure") is True
                    and verified_mcp.call_count == 0
                    and attempt <= config.max_retries
                )
                _attempt_record(attempt_root / "attempt.json", {
                    "attempt": attempt,
                    "phase": "agent",
                    "completed": bool(adapter_result.get("completed")),
                    "startup_failure": bool(adapter_result.get("startup_failure", False)),
                    "mcp_call_count": verified_mcp.call_count,
                    "retry": startup_retry,
                })
                if startup_retry:
                    reset_outcome = phase_runner.run_phase(reset_spec)
                    if not reset_outcome.passed:
                        raise NativeRunError(f"reset phase {reset_outcome.status.value} after retryable startup failure")
                    continue
                if not adapter_result.get("completed"):
                    raise NativeRunError(str(adapter_result.get("error") or "agent phase failed"))
                chosen_mcp = ledger
                verifier_outcome = phase_runner.run_phase(verifier_spec)
                verifier_s += verifier_outcome.duration_s
                if not verifier_outcome.passed:
                    raise NativeRunError(f"verifier phase {verifier_outcome.status.value}")
                break
            else:  # pragma: no cover - bounded loop always exits or breaks
                raise NativeRunError("retry budget exhausted")
        finally:
            reset_error = None
            try:
                reset_outcome = phase_runner.run_phase(reset_spec)
                if not reset_outcome.passed:
                    reset_error = NativeRunError(f"reset phase {reset_outcome.status.value}")
            finally:
                try:
                    monitor.stop()
                except BaseException as exc:
                    reset_error = reset_error or NativeRunError(f"focus monitor cleanup failed: {exc}")
            if reset_error is not None and sys.exc_info()[0] is None:
                raise reset_error

        if monitor.violations:
            for violation in monitor.violations:
                focus_events.append((hooks.clock().isoformat(), {
                    "state": "yielded_to_human",
                    "frontmost_bundle_id": violation.event.bundle_identifier,
                    "target_bundle_id": config.app_bundle_id,
                }))
            raise NativeRunError("focus policy violation observed during native trial")
        if chosen_mcp is None or verifier_outcome is None:
            raise NativeRunError("native trial did not reach verifier completion")

        finished = hooks.clock()
        total_s = max(0.0, (finished - started).total_seconds())
        phase_total = setup_s + agent_s + verifier_s
        if phase_total > total_s:
            total_s = phase_total
            finished = started + timedelta(seconds=total_s)

        config.output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="obench_native_bundle_", dir=str(config.output_dir.parent)) as temp:
            bundle = Path(temp) / config.output_dir.name
            bundle.mkdir()
            task_content = {
                "instruction": _sha256(config.instruction_path),
                "verifier": _canonical_digest(list(config.verifier.argv)),
                "artifacts": [item.path for item in config.artifacts],
            }
            task_sidecar = {
                "schema_version": TASK_SIDECAR_SCHEMA_VERSION,
                "trial_id": config.trial_id,
                "task_id": config.task_id,
                "task_content_sha256": _canonical_digest(task_content),
                "instruction_sha256": task_content["instruction"],
                "verifier_sha256": task_content["verifier"],
            }
            native_sidecar = {
                "schema_version": NATIVE_SIDECAR_SCHEMA_VERSION,
                "trial_id": config.trial_id,
                "task_id": config.task_id,
                "app_bundle_id": config.app_bundle_id,
                "reset_contract_sha256": _canonical_digest(list(config.reset.argv)),
                "success_contract_sha256": _canonical_digest({"verdict_path": config.verdict_path, "artifacts": [item.path for item in config.artifacts]}),
                "final_state_allowlist": [item.path for item in config.artifacts],
            }
            _write_json(bundle / "task/task.json", task_sidecar)
            _write_json(bundle / "task/native.json", native_sidecar)
            preflight_flags = {
                "accessibility": True,
                "screen_recording": True,
                "app_installed": True,
                "display_stable": True,
                "focus_monitor_ready": True,
            }
            lock_environment = {
                "platform": "macos",
                "os": dict(config.environment["os"]),
                "architecture": config.environment["architecture"],
                "hardware_model": config.environment["hardware_model"],
                "app": {
                    "bundle_id": config.app_bundle_id,
                    "version": config.environment["app"]["version"],
                    "build": config.environment["app"]["build"],
                    "code_signature_sha256": config.environment["app"]["code_signature_sha256"],
                },
                "display": dict(config.environment["display"]),
                "preflight": preflight_flags,
            }
            lock = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial_id": config.trial_id,
                "created_at": (started.replace(microsecond=0) if started.microsecond else started).isoformat(),
                "task": {"name": config.task_id, "sidecar_path": "task/task.json", "sidecar_sha256": _sha256(bundle / "task/task.json")},
                "native_sidecar": {"path": "task/native.json", "sha256": _sha256(bundle / "task/native.json")},
                "harness": {"name": config.harness_name, "version": config.harness_version, "version_source": config.harness_version_source},
                "model": {"name": config.model_name, "provider": config.model_provider, "revision": config.model_revision},
                "mcp": {"name": config.mcp_name, "version": config.mcp_version, "transport": "stdio", "server_sha256": _sha256(mcp_executable), "collector_run_id": collector_run_id},
                "environment": lock_environment,
                "budget": {"timeout_s": config.timeout_s, "max_retries": config.max_retries},
                "evidence": {"proxy_required": config.proxy_required},
            }
            _write_json(bundle / "lock.json", lock)
            lock_sha256 = _sha256(bundle / "lock.json")
            _copy_atif(config, bundle, started)
            artifact_entries = _copy_artifacts(config, bundle)
            _write_json(bundle / "artifacts/manifest.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial_id": config.trial_id,
                "lock_sha256": lock_sha256,
                "reviewed": True,
                "contains_sensitive_data": False,
                "artifacts": artifact_entries,
            })
            final_digest = _final_state_digest(artifact_entries)
            try:
                verdict = json.loads((config.workspace / config.verdict_path).read_text(encoding="utf-8"))
                score, checker_exit = verdict["score"], verdict["checker_exit"]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise NativeRunError(f"verifier verdict is unavailable or malformed: {exc}") from exc
            if checker_exit == 0 and float(score) != 1.0 or checker_exit != 0 and float(score) == 1.0:
                raise NativeRunError("verifier score and checker exit disagree")
            (bundle / "mcp").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(chosen_mcp, bundle / "mcp/ledger.jsonl")
            mcp_count = verify_ledger(bundle / "mcp/ledger.jsonl").call_count
            focus_writer = _LedgerWriter(bundle, "focus", config.trial_id, lock_sha256)
            for observed, payload in focus_events:
                focus_writer.append("focus_sample" if payload["state"] == "target_focused" else "focus_yield", payload, observed)
            focus_writer.seal()
            if config.proxy_required:
                proxy_writer = _LedgerWriter(bundle, "proxy", config.trial_id, lock_sha256)
                for usage in proxy_usage:
                    proxy_writer.append("model_usage", usage, finished.isoformat())
                proxy_writer.seal()
            _write_json(bundle / "verifier/reward.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION, "trial_id": config.trial_id,
                "lock_sha256": lock_sha256, "status": "judged", "reward": score,
            })
            _write_json(bundle / "verifier/evidence.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION, "trial_id": config.trial_id,
                "lock_sha256": lock_sha256, "status": "judged", "checker_exit": checker_exit,
                "reward": score, "task_content_sha256": task_sidecar["task_content_sha256"],
                "final_state_sha256": final_digest,
            })
            _write_json(bundle / "result.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION, "trial_id": config.trial_id,
                "lock_sha256": lock_sha256, "status": "completed", "attempts": completed_attempts,
                "retry_count": completed_attempts - 1, "timeout_s": config.timeout_s,
                "started_at": started.isoformat(), "finished_at": finished.isoformat(),
                "timings": {"env_setup_s": setup_s, "agent_s": agent_s, "verifier_s": verifier_s, "total_s": total_s},
                "outcome": {"completed": True, "score": score, "checker_exit": checker_exit,
                    "error": None, "failure_class": "solved" if checker_exit == 0 else "wrong_answer",
                    "failure_reason": None},
                "mcp_event_count": mcp_count, "focus_event_count": len(focus_events),
            })
            _write_manifest(bundle, config.trial_id)
            load_native_trial(bundle)
            os.replace(bundle, config.output_dir)
        row = import_native_trial(config.output_dir, config.results_path)
    return NativeRunOutcome(config.output_dir, config.results_path, row, attempts_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obench native",
        description="Experimental native macOS Computer-Use benchmark runner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run and immediately import one sealed native trial")
    run_parser.add_argument("config", help=f"{CONFIG_SCHEMA_VERSION} TOML config")
    args = parser.parse_args(argv)
    try:
        outcome = run_native(args.config)
    except (NativeRunError, ValueError, OSError) as exc:
        parser.exit(2, f"ERROR {exc}\n")
    print(json.dumps({
        "bundle": str(outcome.bundle_dir),
        "results": str(outcome.results_path),
        "run_id": outcome.row["run_id"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
