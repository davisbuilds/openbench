"""Experimental native macOS Computer-Use trial runner.

This runner owns native host orchestration and emits the evidence contract
validated by :mod:`obench.native_trial`.  It deliberately does not synthesize
Harbor jobs, locks, or execution identity.
"""

from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Iterator, Mapping, Sequence

from .atif import SCHEMA_VERSION as ATIF_SCHEMA_VERSION, assert_valid_trajectory
from .mcp_stdio_collector import (
    COMPUTER_USE_TOOLS,
    CallLedger,
    LedgerIntegrityError,
    collect_stdio,
    verify_ledger,
)
from .native_macos import (
    AppEvidence,
    AppRequirement,
    LeaseOwner,
    MacOSAppInspector,
    MacOSFocusMonitor,
    NativeMacOSHelperResolver,
    PhaseName,
    PhaseSpec,
    PhaseStatus,
    PreflightResult,
    PreflightSpec,
    SubprocessPhaseRunner,
    WholeRunLease,
    run_preflight,
)
from .native_matrix import NativeMatrixError, validate_native_matrix
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
    _write_proxy_cell_metadata,
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
    mcp_collector_run_id: str | None
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
    focus_policy: Mapping[str, Any]
    verifier_oracle_paths: tuple[Path, ...]
    mcp_policy: Mapping[str, Any]
    matrix: Mapping[str, Any] | None


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
    matrix = data.get("matrix")
    normalized_matrix = None
    if matrix is not None:
        if not isinstance(matrix, dict):
            raise NativeRunError("[matrix] must be a table")
        required_matrix = {
            "manifest",
            "plan",
            "plan_sha256",
            "cell_id",
            "cell_sha256",
            "config_sha256",
        }
        if set(matrix) != required_matrix:
            raise NativeRunError(
                f"[matrix] must contain exactly {sorted(required_matrix)!r}"
            )
        manifest_path = resolve(matrix["manifest"], "matrix.manifest")
        plan_path = resolve(matrix["plan"], "matrix.plan")
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validated_plan = validate_native_matrix(plan)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            NativeMatrixError,
        ) as exc:
            raise NativeRunError(f"cannot validate matrix binding: {exc}") from exc
        for field in ("plan_sha256", "cell_sha256", "config_sha256"):
            value = matrix[field]
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise NativeRunError(f"matrix.{field} must be a SHA-256 digest")
        if matrix["plan_sha256"] != validated_plan["plan_sha256"]:
            raise NativeRunError("matrix plan digest does not match the canonical plan")
        matching_cells = [
            cell
            for cell in validated_plan["schedule"]
            if cell["cell_id"] == matrix["cell_id"]
        ]
        if len(matching_cells) != 1:
            raise NativeRunError("matrix.cell_id does not identify one planned cell")
        cell = matching_cells[0]
        for field in ("trial_id", "cell_sha256", "config_sha256"):
            observed = trial_id if field == "trial_id" else matrix[field]
            if observed != cell[field]:
                raise NativeRunError(f"matrix binding has conflicting {field}")
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version")
            != "openbench.computer-use-config-set.v2"
            or not isinstance(manifest.get("cells"), list)
        ):
            raise NativeRunError("matrix manifest is not a v2 config set")
        source_digest = _sha256(source)
        matching_entries = [
            entry
            for entry in manifest["cells"]
            if isinstance(entry, dict)
            and Path(str(entry.get("config", ""))).expanduser().resolve() == source
            and entry.get("plan_sha256") == validated_plan["plan_sha256"]
            and entry.get("cell_id") == cell["cell_id"]
            and entry.get("trial_id") == trial_id
        ]
        if len(matching_entries) != 1:
            raise NativeRunError("matrix manifest does not map this exact planned cell")
        if matching_entries[0].get("runnable_config_sha256") != source_digest:
            raise NativeRunError("matrix runnable config digest does not match the manifest")
        arm = next(
            item for item in validated_plan["arms"] if item["id"] == cell["arm_id"]
        )
        normalized_matrix = {
            "plan_sha256": validated_plan["plan_sha256"],
            "cell_id": cell["cell_id"],
            "cell_sha256": cell["cell_sha256"],
            "config_sha256": cell["config_sha256"],
            "runnable_config_sha256": source_digest,
            "config_identity": arm["config_identity"],
        }
    max_retries = budget.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise NativeRunError("budget.max_retries must be a non-negative integer")
    proxy = data.get("proxy", {})
    if not isinstance(proxy, dict):
        raise NativeRunError("[proxy] must be a table")
    adapter_path = harness.get("candidate")
    focus = table("focus")
    required_foreground = _required_string(
        focus.get("required_foreground_bundle_id"),
        "focus.required_foreground_bundle_id",
    )
    forbidden_bundles = focus.get("forbidden_bundle_ids", [])
    allowed_tiers = focus.get("allowed_delivery_tiers")
    if (
        not isinstance(forbidden_bundles, list)
        or not all(isinstance(value, str) and value for value in forbidden_bundles)
        or len(forbidden_bundles) != len(set(forbidden_bundles))
    ):
        raise NativeRunError("focus.forbidden_bundle_ids must contain unique bundle ids")
    if (
        not isinstance(allowed_tiers, list)
        or not allowed_tiers
        or not all(isinstance(value, str) and value for value in allowed_tiers)
        or len(allowed_tiers) != len(set(allowed_tiers))
    ):
        raise NativeRunError("focus.allowed_delivery_tiers must be a non-empty unique array")
    if required_foreground in forbidden_bundles:
        raise NativeRunError("required foreground bundle cannot also be forbidden")
    raw_oracles = task.get("verifier_oracle_paths")
    if (
        not isinstance(raw_oracles, list)
        or not raw_oracles
        or not all(isinstance(value, str) and value for value in raw_oracles)
    ):
        raise NativeRunError("task.verifier_oracle_paths must be a non-empty path array")
    oracle_paths = tuple(resolve(value, "task.verifier_oracle_paths") for value in raw_oracles)
    if len(oracle_paths) != len(set(oracle_paths)):
        raise NativeRunError("task.verifier_oracle_paths must be unique")
    allowed_tools = mcp.get("allowed_tools", [])
    forbidden_tools = mcp.get("forbidden_tools", [])
    for field, values in (
        ("mcp.allowed_tools", allowed_tools),
        ("mcp.forbidden_tools", forbidden_tools),
    ):
        if (
            not isinstance(values, list)
            or not all(isinstance(value, str) and value for value in values)
            or len(values) != len(set(values))
        ):
            raise NativeRunError(f"{field} must contain unique tool names")
    if not allowed_tools:
        raise NativeRunError("mcp.allowed_tools must be non-empty")
    if set(allowed_tools) & set(forbidden_tools):
        raise NativeRunError("MCP tools cannot be both allowed and forbidden")
    unknown_policy_tools = (
        set(allowed_tools) | set(forbidden_tools)
    ) - set(COMPUTER_USE_TOOLS)
    if unknown_policy_tools:
        raise NativeRunError(
            f"MCP policy contains unknown tools: {sorted(unknown_policy_tools)!r}"
        )
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
        mcp_collector_run_id=(
            _required_string(mcp.get("collector_run_id"), "mcp.collector_run_id")
            if mcp.get("collector_run_id") is not None
            else None
        ),
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
        focus_policy={
            "required_foreground_bundle_id": required_foreground,
            "forbidden_bundle_ids": list(forbidden_bundles),
            "require_foreground_full_agent_phase": bool(
                focus.get("require_foreground_full_agent_phase", True)
            ),
            "forbid_global_delivery": bool(focus.get("forbid_global_delivery", True)),
            "allowed_delivery_tiers": list(allowed_tiers),
        },
        verifier_oracle_paths=oracle_paths,
        mcp_policy={
            "allowed_tools": list(allowed_tools),
            "forbidden_tools": list(forbidden_tools),
        },
        matrix=normalized_matrix,
    )


@dataclass
class NativeRunHooks:
    preflight: Callable[..., PreflightResult] = run_preflight
    phase_runner_factory: Callable[[], SubprocessPhaseRunner] = SubprocessPhaseRunner
    focus_monitor_factory: Callable[[Sequence[str]], Any] = lambda allowed: MacOSFocusMonitor(allowed)
    mcp_owner_probe: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]] = (
        lambda command: _mcp_serve_owners(command)
    )
    mcp_monitor_factory: Callable[
        [
            Sequence[str],
            Callable[[Sequence[str]], Sequence[Mapping[str, Any]]],
            Path,
        ],
        Any,
    ] | None = None
    app_probe: Callable[[AppRequirement], Sequence[AppEvidence]] = (
        lambda requirement: _inspect_setup_app(requirement)
    )
    app_identity_probe: Callable[[Path], Mapping[str, Any]] = (
        lambda app_path: _inspect_setup_app_identity(app_path)
    )
    app_process_probe: Callable[[str, Path, str], Mapping[str, Any]] = (
        lambda bundle_id, executable, cdhash: _inspect_setup_app_process(
            bundle_id, executable, cdhash
        )
    )
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


def _harness_version_matches(expected: str, observed: str | None) -> bool:
    if observed == expected:
        return True
    if observed is None or not observed.endswith(")"):
        return False
    semantic, separator, path_text = observed.rpartition(" (")
    return (
        bool(separator)
        and semantic == expected
        and Path(path_text[:-1]).is_absolute()
    )


def _server_executable(command: Sequence[str]) -> Path:
    first = Path(command[0]).expanduser()
    resolved = first if first.is_absolute() else Path(shutil.which(command[0]) or "")
    if not resolved or not resolved.is_file():
        raise NativeRunError(f"MCP server executable is not a regular file: {command[0]!r}")
    return resolved.resolve()


def _mcp_serve_owners(
    command: Sequence[str],
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], ...]:
    """Return pre-existing configured or computer-use-mcp serve owners."""

    executable = _server_executable(command)
    try:
        completed = command_runner(
            ["ps", "-ww", "-axo", "pid=,ppid=,ucomm=,args="],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRunError(f"cannot enumerate MCP serve owners: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-1000:]
        raise NativeRunError(
            f"cannot enumerate MCP serve owners: ps exited {completed.returncode}: {detail}"
        )

    owners = []
    configured_first = Path(command[0]).expanduser()
    configured_observed = (
        configured_first
        if configured_first.is_absolute()
        else Path(shutil.which(command[0]) or "")
    )
    configured_prefixes = {str(executable)}
    if configured_observed:
        configured_prefixes.add(str(configured_observed))
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.strip().split(None, 3)
        if len(fields) != 4:
            continue
        try:
            pid, parent_pid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        process_name, args = fields[2], fields[3]
        if re.search(r"(?:^|\s)serve(?:\s|$)", args) is None:
            continue
        configured_owner = any(
            args == prefix or args.startswith(prefix + " ")
            for prefix in configured_prefixes
        )
        if process_name != "computer-use-mcp" and not configured_owner:
            continue
        owners.append({
            "pid": pid,
            "parent_pid": parent_pid,
            "process_name": process_name,
            "command": args,
        })
    return tuple(owners)


def _mcp_command_sha256(command: Sequence[str]) -> str:
    encoded = json.dumps(
        list(command),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_mcp_owner_marker(
    path: Path,
    command: Sequence[str],
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise NativeRunError("MCP owner marker is not a regular file")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeRunError(f"cannot read MCP owner marker: {exc}") from exc
    expected_fields = {
        "schema_version",
        "state",
        "collector_pid",
        "child_pid",
        "command_sha256",
    }
    if (
        not isinstance(marker, dict)
        or set(marker) != expected_fields
        or marker.get("schema_version") != "openbench.mcp-process-owner.v1"
        or marker.get("state") not in {"starting", "ready"}
        or type(marker.get("collector_pid")) is not int
        or marker["collector_pid"] <= 0
        or marker.get("command_sha256") != _mcp_command_sha256(command)
        or (
            marker["state"] == "starting"
            and marker.get("child_pid") is not None
        )
        or (
            marker["state"] == "ready"
            and (
                type(marker.get("child_pid")) is not int
                or marker["child_pid"] <= 0
            )
        )
    ):
        raise NativeRunError("MCP owner marker is malformed or command-mismatched")
    return marker


class _McpServeOwnerMonitor:
    def __init__(
        self,
        command: Sequence[str],
        owner_probe: Callable[[Sequence[str]], Sequence[Mapping[str, Any]]],
        owner_path: Path,
        *,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        interval_s: float = 0.1,
    ):
        self.command = tuple(command)
        self.owner_probe = owner_probe
        self.owner_path = owner_path
        self.clock = clock
        self.monotonic = monotonic
        self.interval_s = interval_s
        self._samples: list[dict[str, Any]] = []
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def samples(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(sample) for sample in self._samples)

    @property
    def error(self) -> BaseException | None:
        with self._lock:
            return self._error

    def _set_error(self, error: BaseException) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def _sample(self) -> None:
        owners = tuple(self.owner_probe(self.command))
        marker = _load_mcp_owner_marker(self.owner_path, self.command)
        unrelated_pids = []
        owned_pid = None
        for owner in owners:
            pid = owner.get("pid") if isinstance(owner, Mapping) else None
            parent_pid = (
                owner.get("parent_pid") if isinstance(owner, Mapping) else None
            )
            if (
                type(pid) is not int
                or pid <= 0
                or type(parent_pid) is not int
                or parent_pid <= 0
            ):
                raise NativeRunError(
                    "MCP owner monitor received malformed process evidence"
                )
            if (
                marker is not None
                and parent_pid == marker["collector_pid"]
                and (
                    marker["state"] == "starting"
                    or pid == marker["child_pid"]
                )
            ):
                if owned_pid is not None:
                    raise NativeRunError(
                        "MCP owner monitor found duplicate owned process evidence"
                    )
                owned_pid = pid
            else:
                unrelated_pids.append(pid)
        sample = {
            "observed_at": self.clock().isoformat(),
            "observed_at_monotonic": self.monotonic(),
            "owned_serve_pid": owned_pid,
            "unrelated_serve_pids": sorted(unrelated_pids),
        }
        with self._lock:
            self._samples.append(sample)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._sample()
            except BaseException as exc:
                self._set_error(exc)
                return

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("MCP owner monitor is already started")
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name="openbench-mcp-owner-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=max(1.0, self.interval_s * 5))
        if thread.is_alive():
            raise NativeRunError("MCP owner monitor did not stop")
        if self.error is not None:
            raise NativeRunError(f"MCP owner monitor failed: {self.error}") from self.error
        self._sample()
        self._thread = None
        if any(sample["unrelated_serve_pids"] for sample in self.samples):
            raise NativeRunError(
                "unrelated computer-use-mcp serve owner appeared during agent phase"
            )


def _inspect_setup_app(requirement: AppRequirement) -> Sequence[AppEvidence]:
    helper_path = NativeMacOSHelperResolver().resolve()
    return MacOSAppInspector(helper_path).inspect((requirement,))


def _inspect_setup_app_identity(
    app_path: Path,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if not app_path.is_absolute() or app_path.is_symlink() or not app_path.is_dir():
        raise NativeRunError(
            f"running target app path is not an absolute regular bundle: {app_path}"
        )
    app = app_path.resolve()
    plist_path = app / "Contents/Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
        executable_name = info["CFBundleExecutable"]
    except (OSError, KeyError, plistlib.InvalidFileException) as exc:
        raise NativeRunError(f"cannot inspect running target app bundle: {exc}") from exc
    if not isinstance(executable_name, str) or not executable_name:
        raise NativeRunError("running target app CFBundleExecutable is invalid")
    executable = app / "Contents/MacOS" / executable_name
    if executable.is_symlink() or not executable.is_file():
        raise NativeRunError(
            f"running target app executable is unavailable: {executable}"
        )
    try:
        completed = command_runner(
            ["codesign", "-d", "-r-", str(app)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRunError(f"cannot inspect running target app signature: {exc}") from exc
    if completed.returncode != 0:
        detail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-1000:]
        raise NativeRunError(
            f"running target app codesign inspection exited {completed.returncode}: {detail}"
        )
    requirement_output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    designated = next(
        (
            line.removeprefix("# ").removeprefix("designated =>").strip()
            for line in requirement_output.splitlines()
            if line.startswith(("# designated =>", "designated =>"))
        ),
        "",
    )
    if not designated:
        raise NativeRunError("running target app has no designated code requirement")
    try:
        details = command_runner(
            ["codesign", "-dvvv", str(app)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRunError(f"cannot inspect running target app CDHash: {exc}") from exc
    cdhash_match = re.search(
        r"^CDHash=([0-9a-fA-F]{40})$",
        (details.stderr or "") + (details.stdout or ""),
        re.MULTILINE,
    )
    if details.returncode != 0 or cdhash_match is None:
        raise NativeRunError("running target app has no exact code-signing CDHash")
    binary_sha256 = _sha256(executable)
    signature_sha256 = hashlib.sha256(
        designated.encode("utf-8") + b"\0" + binary_sha256.encode("ascii")
    ).hexdigest()
    return {
        "app": str(app),
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString"),
        "build": str(info.get("CFBundleVersion", "")),
        "executable": str(executable.resolve()),
        "binary_sha256": binary_sha256,
        "signature_sha256": signature_sha256,
        "cdhash": cdhash_match.group(1).lower(),
    }


def _running_app_pids(
    bundle_id: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[int, ...]:
    try:
        found = command_runner(
            ["/usr/bin/lsappinfo", "find", f"bundleID={bundle_id}"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRunError(f"cannot resolve running target app process: {exc}") from exc
    if found.returncode != 0:
        detail = ((found.stderr or "") + (found.stdout or "")).strip()[-1000:]
        raise NativeRunError(
            f"target app process lookup exited {found.returncode}: {detail}"
        )
    app_specifiers: list[str] = []
    for line in (found.stdout or "").splitlines():
        prefix, separator, _ = line.strip().rpartition('-"')
        if separator and prefix.startswith("ASN:"):
            app_specifiers.append(prefix)
        elif line.strip():
            raise NativeRunError("target app process lookup returned malformed evidence")
    pids: list[int] = []
    for app_specifier in app_specifiers:
        try:
            info = command_runner(
                [
                    "/usr/bin/lsappinfo",
                    "info",
                    "-only",
                    "pid",
                    "-app",
                    app_specifier,
                ],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NativeRunError(
                f"cannot inspect running target app process: {exc}"
            ) from exc
        match = re.fullmatch(r'\s*"pid"=(\d+)\s*', info.stdout or "")
        if info.returncode != 0 or match is None or int(match.group(1)) <= 0:
            raise NativeRunError("target app process lookup returned invalid PID evidence")
        pids.append(int(match.group(1)))
    return tuple(sorted(pids))


def _inspect_setup_app_process(
    bundle_id: str,
    executable: Path,
    expected_cdhash: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_cdhash_probe: Callable[[int], str] | None = None,
) -> dict[str, Any]:
    pids_before = _running_app_pids(bundle_id, command_runner=command_runner)
    if len(pids_before) != 1:
        raise NativeRunError(
            "setup did not establish exactly one process-bound target app"
        )
    pid = pids_before[0]
    try:
        executable_stat = executable.stat()
        opened = command_runner(
            ["/usr/sbin/lsof", "-F", "pDint", "-a", "-p", str(pid), "-d", "txt"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRunError(
            f"cannot inspect running target app executable vnode: {exc}"
        ) from exc
    if opened.returncode != 0:
        detail = ((opened.stderr or "") + (opened.stdout or "")).strip()[-1000:]
        raise NativeRunError(
            f"target app executable vnode lookup exited {opened.returncode}: {detail}"
        )
    text_vnodes: set[tuple[int, int]] = set()
    current: dict[str, str] | None = None
    try:
        for line in (opened.stdout or "").splitlines():
            if line.startswith("f"):
                if current is not None and {"D", "i"} <= current.keys():
                    text_vnodes.add((int(current["D"], 0), int(current["i"])))
                current = {}
            elif current is not None and line[:1] in {"D", "i", "n"}:
                current[line[0]] = line[1:]
        if current is not None and {"D", "i"} <= current.keys():
            text_vnodes.add((int(current["D"], 0), int(current["i"])))
    except ValueError as exc:
        raise NativeRunError(
            "target app executable vnode lookup returned malformed evidence"
        ) from exc
    executable_vnode = (executable_stat.st_dev, executable_stat.st_ino)
    if executable_vnode not in text_vnodes:
        raise NativeRunError(
            "running target app process is not using the inspected executable"
        )
    observed_cdhash = (
        process_cdhash_probe(pid)
        if process_cdhash_probe is not None
        else _process_cdhash(pid)
    )
    if observed_cdhash != expected_cdhash:
        raise NativeRunError(
            "running target app process code signature does not match inspected bundle"
        )
    try:
        started = command_runner(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeRunError(
            f"cannot inspect running target app process start time: {exc}"
        ) from exc
    process_start_token = " ".join((started.stdout or "").split())
    if (
        started.returncode != 0
        or re.fullmatch(
            r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
            r"([1-9]|[12][0-9]|3[01]) "
            r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] [0-9]{4}",
            process_start_token,
        )
        is None
    ):
        raise NativeRunError(
            "running target app process start-time lookup returned malformed evidence"
        )
    pids_after = _running_app_pids(bundle_id, command_runner=command_runner)
    if pids_after != pids_before:
        raise NativeRunError("running target app process changed during identity proof")
    return {
        "pid": pid,
        "executable": str(executable.resolve()),
        "device": executable_stat.st_dev,
        "inode": executable_stat.st_ino,
        "cdhash": observed_cdhash,
        "process_start_token": process_start_token,
    }


def _process_cdhash(pid: int) -> str:
    cdhash = (ctypes.c_ubyte * 20)()
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        result = libc.csops(pid, 5, ctypes.byref(cdhash), len(cdhash))
    except AttributeError as exc:
        raise NativeRunError(
            "macOS process code-signature inspection is unavailable"
        ) from exc
    if result != 0:
        error = ctypes.get_errno()
        raise NativeRunError(
            "cannot inspect running target app process code signature: "
            f"{os.strerror(error)}"
        )
    return bytes(cdhash).hex()


def _load_build_manifest(config: NativeRunConfig) -> dict[str, Any] | None:
    manifest_paths = [
        parent / "build-manifest.json"
        for parent in config.source_path.parents
        if (parent / "build-manifest.json").is_file()
    ]
    if len(manifest_paths) > 1:
        raise NativeRunError("multiple Computer-Use build manifests apply to the config")
    if not manifest_paths:
        return None
    try:
        manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeRunError(f"cannot read Computer-Use build manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "openbench.computer-use-build.v1"
        or not isinstance(manifest.get("fixtures"), dict)
    ):
        raise NativeRunError("Computer-Use build manifest is malformed")
    return manifest


def _manifest_fixture(
    manifest: Mapping[str, Any] | None,
    bundle_id: str,
    *,
    required: bool,
) -> dict[str, str] | None:
    fixtures = manifest["fixtures"] if manifest is not None else {}
    matches = [
        value
        for value in fixtures.values()
        if isinstance(value, dict) and value.get("bundle_id") == bundle_id
    ]
    if len(matches) > 1:
        raise NativeRunError(
            f"Computer-Use build manifest has ambiguous identity for {bundle_id!r}"
        )
    if not matches:
        if required:
            raise NativeRunError(
                f"Computer-Use build manifest has no identity for {bundle_id!r}"
            )
        return None
    value = matches[0]
    required_fields = {
        "app",
        "bundle_id",
        "version",
        "build",
        "executable",
        "binary_sha256",
        "signature_sha256",
    }
    if (
        any(
            not isinstance(value.get(field), str) or not value[field]
            for field in required_fields
        )
        or not Path(value["app"]).expanduser().is_absolute()
        or not Path(value["executable"]).expanduser().is_absolute()
    ):
        raise NativeRunError("Computer-Use build manifest app identity is malformed")
    return {field: value[field] for field in required_fields}


def _require_running_app(
    hooks: NativeRunHooks,
    expected: Mapping[str, str],
    *,
    label: str,
) -> tuple[dict[str, Any], datetime]:
    requirement = AppRequirement(expected["bundle_id"], expected["version"])
    evidence = tuple(hooks.app_probe(requirement))
    if any(item.bundle_identifier != requirement.bundle_identifier for item in evidence):
        raise NativeRunError("setup app probe returned an undeclared bundle identifier")
    exact = [
        item
        for item in evidence
        if item.running and item.version == requirement.version
    ]
    if len(exact) != 1:
        observed = [
            {"version": item.version, "running": item.running, "path": item.path}
            for item in evidence
        ]
        raise NativeRunError(
            f"setup did not establish exactly one required {label} app: "
            f"{requirement.bundle_identifier!r} version {requirement.version!r}; "
            f"observed {observed!r}"
        )
    if not exact[0].path:
        raise NativeRunError(f"running {label} app evidence has no bundle path")
    running_path = Path(exact[0].path).expanduser()
    if not running_path.is_absolute():
        raise NativeRunError(f"running {label} app evidence path must be absolute")
    identity = dict(hooks.app_identity_probe(running_path))
    required_identity_fields = {
        "app",
        "bundle_id",
        "version",
        "build",
        "executable",
        "binary_sha256",
        "signature_sha256",
        "cdhash",
    }
    if set(identity) != required_identity_fields:
        raise NativeRunError(
            f"running {label} app identity evidence has unexpected fields"
        )
    observed_app = Path(str(identity["app"])).expanduser()
    observed_executable = Path(str(identity["executable"])).expanduser()
    if (
        not observed_app.is_absolute()
        or observed_app.resolve() != running_path.resolve()
    ):
        raise NativeRunError(
            f"running {label} app identity path does not match process evidence"
        )
    expected_executable_root = observed_app.resolve() / "Contents/MacOS"
    try:
        executable_relative = observed_executable.resolve().relative_to(
            expected_executable_root
        )
    except ValueError as exc:
        raise NativeRunError(
            f"running {label} app executable is outside its bundle"
        ) from exc
    if len(executable_relative.parts) != 1:
        raise NativeRunError(f"running {label} app executable identity is not exact")
    comparisons = {
        field: expected[field]
        for field in (
            "bundle_id",
            "version",
            "build",
            "binary_sha256",
            "signature_sha256",
        )
        if field in expected
    }
    if "app" in expected:
        comparisons["app"] = str(Path(expected["app"]).expanduser().resolve())
    if "executable" in expected:
        comparisons["executable"] = str(
            Path(expected["executable"]).expanduser().resolve()
        )
    mismatches = {
        field: {"expected": value, "observed": identity.get(field)}
        for field, value in comparisons.items()
        if identity.get(field) != value
    }
    if mismatches:
        raise NativeRunError(
            f"running {label} app identity does not match planned identity: {mismatches!r}"
        )
    process_identity = dict(
        hooks.app_process_probe(
            expected["bundle_id"],
            observed_executable,
            str(identity["cdhash"]),
        )
    )
    if set(process_identity) != {
        "pid",
        "executable",
        "device",
        "inode",
        "cdhash",
        "process_start_token",
    }:
        raise NativeRunError(
            f"running {label} app process identity evidence has unexpected fields"
        )
    if (
        type(process_identity["pid"]) is not int
        or process_identity["pid"] <= 0
        or type(process_identity["device"]) is not int
        or type(process_identity["inode"]) is not int
        or process_identity["cdhash"] != identity["cdhash"]
        or not isinstance(process_identity["process_start_token"], str)
        or not process_identity["process_start_token"]
        or Path(str(process_identity["executable"])).resolve()
        != observed_executable.resolve()
    ):
        raise NativeRunError(f"running {label} app process identity is malformed")
    observed_at = hooks.clock()
    return (
        {
            **identity,
            **process_identity,
            "app": str(observed_app.resolve()),
            "executable": str(observed_executable.resolve()),
        },
        observed_at,
    )


def _require_setup_processes(
    hooks: NativeRunHooks,
    config: NativeRunConfig,
) -> tuple[
    tuple[dict[str, Any], datetime],
    tuple[dict[str, Any], datetime],
]:
    manifest = _load_build_manifest(config)
    target_manifest = _manifest_fixture(
        manifest,
        config.app_bundle_id,
        required=manifest is not None,
    )
    configured = config.environment["app"]
    target_expected = {
        "bundle_id": config.app_bundle_id,
        "version": str(configured["version"]),
        "build": str(configured["build"]),
        "signature_sha256": str(configured["code_signature_sha256"]),
    }
    if target_manifest is not None:
        locked_manifest_fields = {
            "bundle_id": config.app_bundle_id,
            "version": str(configured["version"]),
            "build": str(configured["build"]),
            "signature_sha256": str(configured["code_signature_sha256"]),
        }
        manifest_mismatches = {
            field: {
                "configured": value,
                "manifest": target_manifest[field],
            }
            for field, value in locked_manifest_fields.items()
            if target_manifest[field] != value
        }
        if manifest_mismatches:
            raise NativeRunError(
                "Computer-Use build manifest conflicts with locked target "
                f"environment: {manifest_mismatches!r}"
            )
        target_expected.update({
            field: target_manifest[field]
            for field in ("app", "executable", "binary_sha256")
        })
    target_observation = _require_running_app(
        hooks, target_expected, label="target"
    )
    target = target_observation[0]
    foreground_bundle = config.focus_policy["required_foreground_bundle_id"]
    if foreground_bundle == config.app_bundle_id:
        return target_observation, target_observation
    foreground_manifest = _manifest_fixture(
        manifest, foreground_bundle, required=True
    )
    assert foreground_manifest is not None
    foreground_observation = _require_running_app(
        hooks, foreground_manifest, label="foreground"
    )
    foreground = foreground_observation[0]
    if foreground["pid"] == target["pid"]:
        raise NativeRunError("target and foreground roles require separate processes")
    return target_observation, foreground_observation


def _require_setup_app(
    hooks: NativeRunHooks,
    config: NativeRunConfig,
) -> None:
    _require_setup_processes(hooks, config)


def _public_process_identity(
    role: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "role": role,
        "bundle_id": identity["bundle_id"],
        "pid": identity["pid"],
        "version": identity["version"],
        "build": identity["build"],
        "binary_sha256": identity["binary_sha256"],
        "signature_sha256": identity["signature_sha256"],
        "cdhash": identity["cdhash"],
        "process_start_token": identity["process_start_token"],
    }


def _recheck_process_identity(
    hooks: NativeRunHooks,
    identity: Mapping[str, Any],
    *,
    label: str,
) -> datetime:
    observed = dict(
        hooks.app_process_probe(
            str(identity["bundle_id"]),
            Path(str(identity["executable"])),
            str(identity["cdhash"]),
        )
    )
    expected = {
        field: identity[field]
        for field in (
            "pid",
            "executable",
            "device",
            "inode",
            "cdhash",
            "process_start_token",
        )
    }
    if observed != expected:
        raise NativeRunError(
            f"{label} process identity changed during the agent phase"
        )
    return hooks.clock()


def _stop_agent_monitors(
    focus_monitor: Any,
    owner_monitor: Any,
) -> None:
    errors: list[tuple[str, BaseException]] = []
    for label, monitor in (
        ("mcp_owner_monitor_error", owner_monitor),
        ("focus_monitor_error", focus_monitor),
    ):
        try:
            monitor.stop()
        except BaseException as exc:
            errors.append((label, exc))
    if errors:
        primary = errors[0][1]
        for label, error in errors:
            setattr(primary, label, error)
        raise primary


def _run_locked_preflight(
    hooks: NativeRunHooks,
    spec: PreflightSpec,
    mcp_executable: Path,
) -> PreflightResult:
    preflight = hooks.preflight(
        spec,
        computer_use_binary=str(mcp_executable),
    )
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
    return preflight


def _path_inventory(
    path: Path,
    *,
    label: str,
    exclude_runtime_generated: bool = False,
) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise NativeRunError(f"{label} cannot be a symlink: {path}")
    if path.is_file():
        files = [path]
        root = path.parent
    elif path.is_dir():
        root = path
        files = sorted(item for item in path.rglob("*") if item.is_file())
        if any(item.is_symlink() for item in path.rglob("*")):
            raise NativeRunError(f"{label} tree cannot contain symlinks: {path}")
    else:
        raise NativeRunError(f"{label} is not a regular file or directory: {path}")
    if exclude_runtime_generated:
        files = [
            item
            for item in files
            if "__pycache__" not in item.parts
            and item.suffix not in {".pyc", ".pyo"}
        ]
    return [
        {
            "path": item.relative_to(root).as_posix(),
            "size": item.stat().st_size,
            "sha256": _sha256(item),
        }
        for item in files
    ]


def _content_bound_command_digest(
    command: Sequence[str],
    *,
    cwd: Path,
    extra_paths: Sequence[Path] = (),
) -> str:
    executable = _server_executable(command)
    payloads: list[dict[str, Any]] = [
        {"argument_index": 0, "inventory": _path_inventory(executable, label="command executable")}
    ]
    for index, value in enumerate(command[1:], 1):
        if index == 2 and command[1] == "-m":
            continue
        candidate = Path(value).expanduser()
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        if candidate.exists():
            payloads.append({
                "argument_index": index,
                "inventory": _path_inventory(candidate.resolve(), label="command payload"),
            })
    if len(command) >= 3 and command[1] == "-m":
        resolver = (
            "import importlib.util,json,sys;"
            "s=importlib.util.find_spec(sys.argv[1]);"
            "print(json.dumps(None if s is None else {"
            "'origin':s.origin,'locations':list(s.submodule_search_locations or [])}))"
        )
        try:
            completed = subprocess.run(
                [str(executable), "-c", resolver, command[2]],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            resolved_module = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise NativeRunError(
                f"cannot resolve interpreted MCP module {command[2]!r}: {exc}"
            ) from exc
        if not isinstance(resolved_module, dict):
            raise NativeRunError(f"cannot resolve interpreted MCP module {command[2]!r}")
        locations = resolved_module.get("locations")
        origin = resolved_module.get("origin")
        if isinstance(locations, list) and locations:
            module_path = Path(locations[0]).resolve()
        elif isinstance(origin, str) and origin not in {"built-in", "frozen"}:
            module_path = Path(origin).resolve()
        else:
            raise NativeRunError(
                f"interpreted MCP module has no hashable payload: {command[2]!r}"
            )
        payloads.append({
            "module": command[2],
            "inventory": _path_inventory(
                module_path,
                label="interpreted MCP module",
                exclude_runtime_generated=True,
            ),
        })
    for index, path in enumerate(extra_paths):
        payloads.append({
            "oracle_index": index,
            "inventory": _path_inventory(path, label="verifier oracle"),
        })
    return _canonical_digest({"argv": list(command), "payloads": payloads})


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
    collector_run_id = (
        config.mcp_collector_run_id or f"{config.trial_id}-mcp"
    )
    return PhaseSpec(
        name,
        _expand_command(item.argv, config),
        item.timeout_s,
        # Setup owns initial workspace materialization. Use the immutable
        # config directory until that workspace exists so a generated cell is
        # runnable with its advertised single command.
        cwd=str(
            config.source_path.parent
            if name is PhaseName.SETUP and not config.workspace.exists()
            else config.workspace
        ),
        env={
            **os.environ,
            "OPENBENCH_NATIVE_TRIAL_ID": config.trial_id,
            "OPENBENCH_NATIVE_TASK_ID": config.task_id,
            "OPENBENCH_NATIVE_MCP_COLLECTOR_RUN_ID": collector_run_id,
        },
    )


def _locked_environment(config: NativeRunConfig) -> dict[str, Any]:
    return {
        "platform": "macos",
        "os": dict(config.environment["os"]),
        "architecture": config.environment["architecture"],
        "hardware_model": config.environment["hardware_model"],
        "app": {
            "bundle_id": config.app_bundle_id,
            "version": config.environment["app"]["version"],
            "build": config.environment["app"]["build"],
            "code_signature_sha256": config.environment["app"][
                "code_signature_sha256"
            ],
        },
        "display": dict(config.environment["display"]),
        "preflight": {
            "accessibility": True,
            "screen_recording": True,
            "app_installed": True,
            "display_stable": True,
            "focus_monitor_ready": True,
        },
    }


def _matrix_seal(config: NativeRunConfig) -> dict[str, Any] | None:
    if config.matrix is None:
        return None
    return {
        key: config.matrix[key]
        for key in (
            "plan_sha256",
            "cell_id",
            "cell_sha256",
            "config_sha256",
            "runnable_config_sha256",
        )
    }


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
        server_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("OPENBENCH_")
            and key != "CUB_MCP_COMMAND"
        }
        result = collect_stdio(
            command,
            ledger_path=os.environ["OPENBENCH_NATIVE_MCP_LEDGER"],
            run_id=os.environ["OPENBENCH_NATIVE_MCP_COLLECTOR_RUN_ID"],
            trial_id=os.environ["OPENBENCH_NATIVE_TRIAL_ID"],
            owner_path=os.environ["OPENBENCH_NATIVE_MCP_OWNER_PATH"],
            env=server_env,
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


_STEP_TOKEN_METRICS = ("prompt_tokens", "cached_tokens", "completion_tokens")


def _numeric_metric(value: Any, field: str, *, integer: bool) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or (integer and not isinstance(value, int))
    ):
        kind = "non-negative integer" if integer else "non-negative number"
        raise NativeRunError(f"{field} must be a {kind}")
    return value


def _public_step_metrics(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, int | float] = {}
    for field in _STEP_TOKEN_METRICS:
        if field in value:
            projected[field] = _numeric_metric(
                value[field], f"ATIF metrics.{field}", integer=True
            )
    if "cost_usd" in value:
        projected["cost_usd"] = _numeric_metric(
            value["cost_usd"], "ATIF metrics.cost_usd", integer=False
        )
    return projected


def _copy_atif(
    config: NativeRunConfig,
    bundle: Path,
    started: datetime,
    *,
    source_root: Path,
) -> dict[str, Any]:
    source = source_root / config.atif_path
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
    public_steps = []
    for step in trajectory.get("steps", []):
        projected = {
            "step_id": len(public_steps) + 1,
            "source": step.get("source"),
            "message": "[content omitted from public native evidence]",
            "timestamp": step.get("timestamp", started.isoformat()),
        }
        if step.get("source") == "agent":
            projected["model_name"] = config.model_name
            metrics = _public_step_metrics(step.get("metrics"))
            if metrics:
                projected["metrics"] = metrics
        public_steps.append(projected)
    final_metrics: dict[str, int | float] = {
        "total_steps": len(public_steps),
    }
    for field in _STEP_TOKEN_METRICS:
        final_name = "total_" + field
        final_metrics[final_name] = sum(
            int(step.get("metrics", {}).get(field, 0))
            for step in public_steps
        )
    costs = [
        float(step["metrics"]["cost_usd"])
        for step in public_steps
        if "cost_usd" in step.get("metrics", {})
    ]
    if costs:
        final_metrics["total_cost_usd"] = sum(costs)
    trajectory = {
        "schema_version": ATIF_SCHEMA_VERSION,
        "trajectory_id": config.trial_id,
        "agent": trajectory["agent"],
        "steps": public_steps,
        "final_metrics": final_metrics,
    }
    assert_valid_trajectory(trajectory)
    _write_json(bundle / "agent/trajectory.json", trajectory)
    return trajectory


def _copy_artifacts(
    config: NativeRunConfig, bundle: Path, *, source_root: Path
) -> list[dict[str, Any]]:
    entries = []
    for item in config.artifacts:
        source = source_root / item.source
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


def _snapshot_final_evidence(
    config: NativeRunConfig,
    destination: Path,
    *,
    include_verdict: bool,
) -> str:
    sources = [config.atif_path]
    if include_verdict:
        sources.append(config.verdict_path)
    sources.extend(item.source for item in config.artifacts)
    for relative in sources:
        source = config.workspace / relative
        if not source.is_file() or source.is_symlink():
            phase = "judged" if include_verdict else "terminal"
            raise NativeRunError(f"{phase} evidence is unavailable: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return _canonical_digest(
        _path_inventory(destination, label="final evidence snapshot")
    )


def _verify_mcp_policy(path: Path, policy: Mapping[str, Any]) -> None:
    allowed = set(policy["allowed_tools"])
    forbidden = set(policy["forbidden_tools"])
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ][:-1]
    observed = {record["tool"] for record in records}
    if "<unrecognized>" in observed:
        raise NativeRunError("MCP tool policy violation: unrecognized tool observed")
    blocked = observed & forbidden
    outside = observed - allowed if allowed else set()
    if blocked or outside:
        raise NativeRunError(
            "MCP tool policy violation: "
            f"forbidden={sorted(blocked)!r}, outside_allowlist={sorted(outside)!r}"
        )


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
    proxy_context = {"ledger_dir": directory}
    _write_proxy_cell_metadata(
        proxy_context,
        token,
        proxy_harness,
        config.model_name,
    )
    metadata_path = directory / f"{token}.meta.json"
    if not metadata_path.is_file():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        raise NativeRunError("native counting proxy metadata was not persisted")
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
        try:
            yield context
        except BaseException:
            server.abort_cell(token)
            raise
        else:
            server.seal_cell(token, timeout_s=5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _proxy_evidence(
    context: Mapping[str, Any] | None, *, allow_aborted: bool = False
) -> tuple[list[dict[str, int]], dict[str, Any] | None]:
    if context is None:
        return [], None
    rows = read_proxy_ledger(context["ledger_dir"], context["token"])
    if not rows or rows[-1].get("record_type") != "ledger_seal":
        raise NativeRunError("counting proxy ledger is not durably sealed")
    seal = rows[-1]
    if seal.get("state") != "SEALED":
        if not (
            allow_aborted
            and seal.get("state") == "ABORTED"
            and seal.get("complete") is False
            and isinstance(seal.get("incomplete_in_flight_count"), int)
            and seal["incomplete_in_flight_count"] >= 0
        ):
            raise NativeRunError("counting proxy ledger is incomplete")
    measured: dict[str, Any] = {}
    apply_proxy_ledger(measured, rows[:-1])
    if measured.get("proxy_capture_truncated") or measured.get("token_basis_proxy") != "proxy_measured":
        if measured.get("tokens_proxy_calls") == 0:
            usage = []
        else:
            raise NativeRunError("counting proxy evidence is incomplete")
    else:
        usage = [{
            "input_tokens": int(measured.get("tokens_proxy_input_uncached") or 0)
            + int(measured.get("tokens_proxy_cache_read") or 0),
            "cached_tokens": int(measured.get("tokens_proxy_cache_read") or 0),
            "output_tokens": int(measured.get("tokens_proxy_output") or 0),
        }]
    terminal = {
        "state": seal["state"],
        "complete": seal.get("complete", seal["state"] == "SEALED"),
        "incomplete_in_flight_count": seal.get(
            "incomplete_in_flight_count", 0
        ),
    }
    return usage, terminal


def _proxy_usage(
    context: Mapping[str, Any] | None, *, allow_aborted: bool = False
) -> list[dict[str, int]]:
    usage, _terminal = _proxy_evidence(
        context,
        allow_aborted=allow_aborted,
    )
    return usage


def _attempt_record(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_json(path, value)


def _verify_mcp_ledger_after_shutdown(
    path: Path, *, timeout_s: float = 5.0, poll_s: float = 0.05
):
    """Wait briefly for the collector's graceful terminal seal, then verify."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if lines:
                terminal = json.loads(lines[-1])
                if (
                    isinstance(terminal, dict)
                    and terminal.get("record_type") == "ledger_seal"
                ):
                    return verify_ledger(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                return verify_ledger(path)
            except (OSError, LedgerIntegrityError) as exc:
                raise NativeRunError(
                    f"MCP collector evidence did not seal cleanly: {exc}"
                ) from exc
        time.sleep(min(poll_s, remaining))


def _record_focus_monitor_diagnostic(
    path: Path, attempt: int, monitor: Any
) -> None:
    _attempt_record(
        path,
        {
            "attempt": attempt,
            "events": [
                {
                    "bundle_id": event.bundle_identifier,
                    "pid": event.pid,
                    "observed_at": event.observed_at,
                    "source_monotonic_ns": event.source_monotonic_ns,
                    "source_sequence": event.source_sequence,
                    "sample_kind": event.sample_kind,
                    "session_status": event.session_status,
                    "screen_unlocked": event.screen_unlocked,
                }
                for event in monitor.events
            ],
        },
    )


def run_native(config_or_path: NativeRunConfig | str | os.PathLike[str], *, hooks: NativeRunHooks | None = None) -> NativeRunOutcome:
    config = config_or_path if isinstance(config_or_path, NativeRunConfig) else load_config(config_or_path)
    hooks = hooks or NativeRunHooks()
    if not config.instruction_path.exists():
        raise NativeRunError(
            f"required native input does not exist: {config.instruction_path}"
        )
    if config.output_dir.exists():
        raise NativeRunError(f"native output already exists: {config.output_dir}")
    attempts_dir = config.output_dir.with_name(config.output_dir.name + ".attempts")
    if attempts_dir.exists():
        raise NativeRunError(f"native attempts output already exists: {attempts_dir}")

    owner = LeaseOwner.current(config.trial_id)
    with WholeRunLease(config.lease_path, owner=owner):
        adapter = (hooks.adapter_loader or _load_adapter)(config)
        version_probe = hooks.version_probe or _probe_version
        observed_version = version_probe(config, adapter)
        if not _harness_version_matches(config.harness_version, observed_version):
            raise NativeRunError(
                f"harness version mismatch: expected {config.harness_version!r}, observed {observed_version!r}"
            )
        mcp_executable = _server_executable(config.mcp_command)
        serve_owners = tuple(hooks.mcp_owner_probe(config.mcp_command))
        if serve_owners:
            owner_pids = sorted(
                str(owner.get("pid", "unknown"))
                if isinstance(owner, Mapping)
                else "unknown"
                for owner in serve_owners
            )
            raise NativeRunError(
                f"unrelated computer-use-mcp serve owners are already running: {owner_pids!r}"
            )
        preflight_spec = PreflightSpec(
            computer_use_version=config.mcp_version,
            computer_use_bundle_identifier=config.environment.get("mcp_bundle_id"),
        )
        _run_locked_preflight(hooks, preflight_spec, mcp_executable)
        phase_runner = hooks.phase_runner_factory()
        setup_spec = _phase(config, PhaseName.SETUP, config.setup)
        verifier_spec = _phase(config, PhaseName.VERIFIER, config.verifier)
        reset_spec = _phase(config, PhaseName.RESET, config.reset)
        instruction_sha256 = _sha256(config.instruction_path)
        verifier_content_sha256 = _content_bound_command_digest(
            verifier_spec.argv,
            cwd=(
                config.workspace
                if config.workspace.exists()
                else config.source_path.parent
            ),
            extra_paths=config.verifier_oracle_paths,
        )
        mcp_content_sha256 = _content_bound_command_digest(
            config.mcp_command,
            cwd=config.source_path.parent,
        )
        collector_run_id = (
            config.mcp_collector_run_id or f"{config.trial_id}-mcp"
        )
        task_content = {
            "instruction": instruction_sha256,
            "verifier": verifier_content_sha256,
            "artifacts": [item.path for item in config.artifacts],
        }
        lock_environment = _locked_environment(config)
        if config.matrix is not None:
            actual_identity = {
                "task": {
                    "name": config.task_id,
                    "content_sha256": _canonical_digest(task_content),
                },
                "harness": {
                    "name": config.harness_name,
                    "version": config.harness_version,
                    "version_source": config.harness_version_source,
                },
                "model": {
                    "name": config.model_name,
                    "provider": config.model_provider,
                    "revision": config.model_revision,
                },
                "mcp": {
                    "name": config.mcp_name,
                    "version": config.mcp_version,
                    "transport": "stdio",
                    "server_sha256": mcp_content_sha256,
                    "collector_run_id": collector_run_id,
                },
                "arm_config": {"environment": lock_environment},
            }
            if actual_identity != config.matrix["config_identity"]:
                raise NativeRunError(
                    "runnable config identity does not match its planned matrix arm"
                )
        instruction = config.instruction_path.read_text(encoding="utf-8")
        started = hooks.clock()
        start_mono = hooks.monotonic()
        setup_s = agent_s = verifier_s = 0.0
        chosen_mcp: Path | None = None
        proxy_usage: list[dict[str, int]] = []
        proxy_terminal: dict[str, Any] | None = None
        focus_events: list[tuple[str, Mapping[str, Any]]] = []
        process_events: list[tuple[str, str, Mapping[str, Any]]] = []
        adapter_result: Mapping[str, Any] | None = None
        verifier_outcome = None
        evidence_snapshot: Path | None = None
        evidence_snapshot_sha256: str | None = None
        result_status: str | None = None
        outcome_error: str | None = None
        failure_reason: str | None = None
        completed_attempts = 0
        agent_started_at: datetime | None = None
        agent_finished_at: datetime | None = None
        focus_violations = []
        try:
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
                if not config.workspace.is_dir() or config.workspace.is_symlink():
                    raise NativeRunError(
                        "setup did not materialize a regular native workspace: "
                        f"{config.workspace}"
                    )

                (
                    (target_identity, target_setup_observed_at),
                    (foreground_identity, foreground_setup_observed_at),
                ) = _require_setup_processes(hooks, config)
                process_events.extend(
                    (
                        observed_at.isoformat(),
                        "process_identity",
                        {
                            "attempt": attempt,
                            **_public_process_identity(role, identity),
                            "phase": "setup",
                        },
                    )
                    for role, identity, observed_at in (
                        (
                            "target",
                            target_identity,
                            target_setup_observed_at,
                        ),
                        (
                            "foreground",
                            foreground_identity,
                            foreground_setup_observed_at,
                        ),
                    )
                )

                launcher = attempt_root / "computer-use-mcp-collector"
                ledger = attempt_root / "mcp-ledger.jsonl"
                owner_path = attempt_root / "mcp-process-owner.json"
                _collector_launcher(launcher)
                env = {
                    config.mcp_client_command_env: str(launcher),
                    "OPENBENCH_NATIVE_MCP_SERVER_COMMAND": json.dumps(list(config.mcp_command)),
                    "OPENBENCH_NATIVE_MCP_LEDGER": str(ledger),
                    "OPENBENCH_NATIVE_MCP_COLLECTOR_RUN_ID": collector_run_id,
                    "OPENBENCH_NATIVE_MCP_OWNER_PATH": str(owner_path),
                    "OPENBENCH_NATIVE_MCP_ALLOWED_TOOLS": json.dumps(
                        config.mcp_policy["allowed_tools"],
                        separators=(",", ":"),
                    ),
                    "OPENBENCH_NATIVE_MCP_ARGUMENT_POLICY": json.dumps(
                        {
                            "forbid_focus_change": config.focus_policy[
                                "require_foreground_full_agent_phase"
                            ],
                            "forbid_global_delivery": config.focus_policy[
                                "forbid_global_delivery"
                            ],
                        },
                        separators=(",", ":"),
                    ),
                    "OPENBENCH_NATIVE_TRIAL_ID": config.trial_id,
                }
                token = f"native-{attempt}"
                proxy_dir = attempt_root / "proxy-raw"
                proxy_harness = (
                    getattr(adapter, "proxy_adapter", None)
                    or getattr(adapter, "base_adapter", None)
                    or config.harness_name
                )
                monitor = hooks.focus_monitor_factory(
                    (config.focus_policy["required_foreground_bundle_id"],)
                )
                owner_monitor = (
                    hooks.mcp_monitor_factory(
                        config.mcp_command, hooks.mcp_owner_probe, owner_path
                    )
                    if hooks.mcp_monitor_factory is not None
                    else _McpServeOwnerMonitor(
                        config.mcp_command,
                        hooks.mcp_owner_probe,
                        owner_path,
                        clock=hooks.clock,
                        monotonic=hooks.monotonic,
                    )
                )
                with _managed_proxy(
                    config, proxy_dir, hooks, token, proxy_harness
                ) as proxy_context:
                    if proxy_context:
                        env.update(proxy_context["env"])
                    with _temporary_environ(env):
                        monitor.start()
                        try:
                            owner_monitor.start()
                        except BaseException:
                            monitor.stop()
                            raise
                        attempt_agent_started_at = hooks.clock()
                        process_events.append((
                            attempt_agent_started_at.isoformat(),
                            "agent_boundary",
                            {"attempt": attempt, "boundary": "start"},
                        ))
                        try:
                            adapter_result = adapter.run(
                                instruction, str(config.workspace), config.model_name, int(config.timeout_s)
                            )
                        except BaseException as adapter_error:
                            attempt_agent_finished_at = hooks.clock()
                            process_events.append((
                                attempt_agent_finished_at.isoformat(),
                                "agent_boundary",
                                {"attempt": attempt, "boundary": "finish"},
                            ))
                            try:
                                _stop_agent_monitors(monitor, owner_monitor)
                                _record_focus_monitor_diagnostic(
                                    attempt_root / "focus-monitor.json",
                                    attempt,
                                    monitor,
                                )
                            except BaseException as monitor_error:
                                setattr(
                                    adapter_error,
                                    "agent_monitor_error",
                                    monitor_error,
                                )
                                if hasattr(
                                    monitor_error, "focus_monitor_error"
                                ):
                                    setattr(
                                        adapter_error,
                                        "focus_monitor_error",
                                        monitor_error.focus_monitor_error,
                                    )
                                if hasattr(
                                    monitor_error, "mcp_owner_monitor_error"
                                ):
                                    setattr(
                                        adapter_error,
                                        "mcp_owner_monitor_error",
                                        monitor_error.mcp_owner_monitor_error,
                                    )
                            raise
                        else:
                            attempt_agent_finished_at = hooks.clock()
                            process_events.append((
                                attempt_agent_finished_at.isoformat(),
                                "agent_boundary",
                                {"attempt": attempt, "boundary": "finish"},
                            ))
                            _stop_agent_monitors(monitor, owner_monitor)
                            _record_focus_monitor_diagnostic(
                                attempt_root / "focus-monitor.json",
                                attempt,
                                monitor,
                            )
                        if (
                            proxy_context
                            and isinstance(adapter_result, Mapping)
                            and not adapter_result.get("completed")
                        ):
                            proxy_context["server"].abort_cell(token)
                for sample in owner_monitor.samples:
                    process_events.append((
                        sample["observed_at"],
                        "mcp_owner_sample",
                        {
                            "attempt": attempt,
                            "owned_serve_pid": sample["owned_serve_pid"],
                            "unrelated_serve_pids": sample[
                                "unrelated_serve_pids"
                            ],
                        },
                    ))
                if not any(
                    sample["owned_serve_pid"] is not None
                    for sample in owner_monitor.samples
                ):
                    raise NativeRunError(
                        "MCP owner monitor never observed the benchmark-owned "
                        "serve process"
                    )
                if attempt_agent_finished_at < attempt_agent_started_at:
                    raise NativeRunError(
                        "agent phase wall-clock boundaries are not ordered"
                    )
                agent_started_at = attempt_agent_started_at
                agent_finished_at = attempt_agent_finished_at
                agent_s += (
                    attempt_agent_finished_at - attempt_agent_started_at
                ).total_seconds()
                focus_violations.extend(monitor.violations)
                for event in monitor.events:
                    if event.observed_at is None:
                        raise NativeRunError(
                            "focus helper event omitted its wall-clock timestamp"
                        )
                    if (
                        event.bundle_identifier
                        == config.focus_policy["required_foreground_bundle_id"]
                        and event.pid != foreground_identity["pid"]
                    ):
                        raise NativeRunError(
                            "focus sample does not identify the setup-established "
                            "foreground process"
                        )
                    focus_events.append((event.observed_at, {
                        "attempt": attempt,
                        "state": "observed",
                        "frontmost_bundle_id": event.bundle_identifier,
                        "frontmost_pid": event.pid,
                        "target_bundle_id": config.app_bundle_id,
                        "target_pid": target_identity["pid"],
                    }))
                target_terminal_observed_at = _recheck_process_identity(
                    hooks, target_identity, label="target"
                )
                if foreground_identity is not target_identity:
                    foreground_terminal_observed_at = _recheck_process_identity(
                        hooks, foreground_identity, label="foreground"
                    )
                else:
                    foreground_terminal_observed_at = (
                        target_terminal_observed_at
                    )
                process_events.extend(
                    (
                        observed_at.isoformat(),
                        "process_identity",
                        {
                            "attempt": attempt,
                            **_public_process_identity(role, identity),
                            "phase": "terminal",
                        },
                    )
                    for role, identity, observed_at in (
                        (
                            "target",
                            target_identity,
                            target_terminal_observed_at,
                        ),
                        (
                            "foreground",
                            foreground_identity,
                            foreground_terminal_observed_at,
                        ),
                    )
                )
                proxy_usage, proxy_terminal = _proxy_evidence(
                    proxy_context,
                    allow_aborted=not bool(
                        isinstance(adapter_result, Mapping)
                        and adapter_result.get("completed")
                    ),
                )
                if not isinstance(adapter_result, Mapping):
                    raise NativeRunError("adapter returned a non-object result")
                if not ledger.exists():
                    if adapter_result.get("startup_failure") is True:
                        _empty_mcp_ledger(ledger, collector_run_id, config.trial_id)
                    else:
                        raise NativeRunError("harness did not launch the configured MCP collector")
                verified_mcp = _verify_mcp_ledger_after_shutdown(ledger)
                startup_retry = (
                    adapter_result.get("startup_failure") is True
                    and verified_mcp.call_count == 0
                    and (
                        sum(sum(item.values()) for item in proxy_usage) == 0
                        if config.proxy_required
                        else adapter_result.get("tokens") == 0
                    )
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
                if adapter_result.get("startup_failure") is True and attempt <= config.max_retries:
                    raise NativeRunError(
                        "startup retry refused because zero-token evidence is absent"
                    )
                if not adapter_result.get("completed"):
                    if adapter_result.get("terminal_status") != "timeout":
                        raise NativeRunError(
                            str(adapter_result.get("error") or "agent phase failed")
                        )
                    chosen_mcp = ledger
                    if focus_violations:
                        raise NativeRunError(
                            "focus policy violation observed during agent phase"
                        )
                    result_status = "timeout"
                    outcome_error = f"timeout after {config.timeout_s:g}s"
                    failure_reason = "deadline_exceeded"
                    evidence_snapshot = attempt_root / "terminal"
                    evidence_snapshot_sha256 = _snapshot_final_evidence(
                        config,
                        evidence_snapshot,
                        include_verdict=False,
                    )
                    break
                chosen_mcp = ledger
                if focus_violations:
                    raise NativeRunError(
                        "focus policy violation observed during agent phase"
                    )
                verifier_outcome = phase_runner.run_phase(verifier_spec)
                verifier_s += verifier_outcome.duration_s
                if verifier_outcome.status not in {
                    PhaseStatus.PASSED,
                    PhaseStatus.FAILED,
                }:
                    raise NativeRunError(
                        f"verifier phase {verifier_outcome.status.value}"
                    )
                if (
                    verifier_outcome.status == PhaseStatus.FAILED
                    and not (config.workspace / config.verdict_path).is_file()
                ):
                    raise NativeRunError(
                        "verifier phase failed without a verdict"
                    )
                result_status = "completed"
                evidence_snapshot = attempt_root / "judged"
                evidence_snapshot_sha256 = _snapshot_final_evidence(
                    config,
                    evidence_snapshot,
                    include_verdict=True,
                )
                break
            else:  # pragma: no cover - bounded loop always exits or breaks
                raise NativeRunError("retry budget exhausted")
        finally:
            reset_error = None
            reset_outcome = phase_runner.run_phase(reset_spec)
            if not reset_outcome.passed:
                reset_error = NativeRunError(f"reset phase {reset_outcome.status.value}")
            if reset_error is not None and sys.exc_info()[0] is None:
                raise reset_error

        terminal_observed_version = version_probe(config, adapter)
        if not _harness_version_matches(
            config.harness_version, terminal_observed_version
        ):
            raise NativeRunError(
                "harness version changed during native execution: "
                f"expected {config.harness_version!r}, "
                f"observed {terminal_observed_version!r}"
            )
        if terminal_observed_version != observed_version:
            raise NativeRunError(
                "harness executable identity changed during native execution: "
                f"started {observed_version!r}, "
                f"finished {terminal_observed_version!r}"
            )

        if focus_violations:
            for violation in focus_violations:
                focus_events.append((hooks.clock().isoformat(), {
                    "attempt": completed_attempts,
                    "state": "yielded_to_human",
                    "frontmost_bundle_id": violation.event.bundle_identifier,
                    "frontmost_pid": violation.event.pid,
                    "target_bundle_id": config.app_bundle_id,
                    "target_pid": (
                        target_identity["pid"]
                        if "target_identity" in locals()
                        else None
                    ),
                }))
            raise NativeRunError("focus policy violation observed during native trial")
        if not focus_events:
            raise NativeRunError("focus helper produced no observed foreground samples")
        if chosen_mcp is None or result_status is None:
            raise NativeRunError("native trial did not reach a terminal outcome")
        if result_status == "completed" and verifier_outcome is None:
            raise NativeRunError("completed native trial has no verifier outcome")
        if evidence_snapshot is None or evidence_snapshot_sha256 is None:
            raise NativeRunError("native trial has no final evidence snapshot")
        if (
            _canonical_digest(
                _path_inventory(evidence_snapshot, label="final evidence snapshot")
            )
            != evidence_snapshot_sha256
        ):
            raise NativeRunError("reset mutated the captured final evidence")
        if _sha256(config.instruction_path) != instruction_sha256:
            raise NativeRunError("instruction bytes changed during native execution")
        if (
            _content_bound_command_digest(
                verifier_spec.argv,
                cwd=config.workspace,
                extra_paths=config.verifier_oracle_paths,
            )
            != verifier_content_sha256
        ):
            raise NativeRunError("verifier or oracle bytes changed during native execution")
        if (
            _content_bound_command_digest(
                config.mcp_command,
                cwd=config.source_path.parent,
            )
            != mcp_content_sha256
        ):
            raise NativeRunError("MCP command payload changed during native execution")
        _verify_mcp_policy(chosen_mcp, config.mcp_policy)

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
                "reset_contract_sha256": _canonical_digest(
                    list(config.reset.argv)
                    if config.matrix is None
                    else {
                        "argv": list(config.reset.argv),
                        "matrix": _matrix_seal(config),
                    }
                ),
                "success_contract_sha256": _canonical_digest({"verdict_path": config.verdict_path, "artifacts": [item.path for item in config.artifacts]}),
                "final_state_allowlist": [item.path for item in config.artifacts],
                "focus_policy": dict(config.focus_policy),
            }
            _write_json(bundle / "task/task.json", task_sidecar)
            _write_json(bundle / "task/native.json", native_sidecar)
            lock = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial_id": config.trial_id,
                "created_at": (started.replace(microsecond=0) if started.microsecond else started).isoformat(),
                "task": {"name": config.task_id, "sidecar_path": "task/task.json", "sidecar_sha256": _sha256(bundle / "task/task.json")},
                "native_sidecar": {"path": "task/native.json", "sha256": _sha256(bundle / "task/native.json")},
                "harness": {"name": config.harness_name, "version": config.harness_version, "version_source": config.harness_version_source},
                "model": {"name": config.model_name, "provider": config.model_provider, "revision": config.model_revision},
                "mcp": {"name": config.mcp_name, "version": config.mcp_version, "transport": "stdio", "server_sha256": mcp_content_sha256, "collector_run_id": collector_run_id},
                "environment": lock_environment,
                "budget": {"timeout_s": config.timeout_s, "max_retries": config.max_retries},
                "evidence": {
                    "proxy_required": config.proxy_required,
                    "process_monitor_required": True,
                },
            }
            _write_json(bundle / "lock.json", lock)
            lock_sha256 = _sha256(bundle / "lock.json")
            _copy_atif(config, bundle, started, source_root=evidence_snapshot)
            artifact_entries = _copy_artifacts(
                config, bundle, source_root=evidence_snapshot
            )
            _write_json(bundle / "artifacts/manifest.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial_id": config.trial_id,
                "lock_sha256": lock_sha256,
                "reviewed": True,
                "contains_sensitive_data": False,
                "artifacts": artifact_entries,
            })
            final_digest = _final_state_digest(artifact_entries)
            score: int | float | None = None
            checker_exit: int | None = None
            if result_status == "completed":
                try:
                    verdict = json.loads(
                        (
                            evidence_snapshot / config.verdict_path
                        ).read_text(encoding="utf-8")
                    )
                    score, checker_exit = (
                        verdict["score"],
                        verdict["checker_exit"],
                    )
                except (
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                ) as exc:
                    raise NativeRunError(
                        f"verifier verdict is unavailable or malformed: {exc}"
                    ) from exc
                if (
                    checker_exit == 0
                    and float(score) != 1.0
                    or checker_exit != 0
                    and float(score) == 1.0
                ):
                    raise NativeRunError(
                        "verifier score and checker exit disagree"
                    )
            (bundle / "mcp").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(chosen_mcp, bundle / "mcp/ledger.jsonl")
            mcp_count = verify_ledger(bundle / "mcp/ledger.jsonl").call_count
            focus_writer = _LedgerWriter(bundle, "focus", config.trial_id, lock_sha256)
            for observed, payload in focus_events:
                focus_writer.append("focus_sample" if payload["state"] == "observed" else "focus_yield", payload, observed)
            focus_writer.seal()
            process_writer = _LedgerWriter(
                bundle, "process", config.trial_id, lock_sha256
            )
            for observed, kind, payload in sorted(
                process_events, key=lambda item: item[0]
            ):
                process_writer.append(kind, payload, observed)
            process_writer.seal()
            if config.proxy_required:
                if proxy_terminal is None:
                    raise NativeRunError(
                        "required counting proxy terminal evidence is absent"
                    )
                proxy_writer = _LedgerWriter(bundle, "proxy", config.trial_id, lock_sha256)
                for usage in proxy_usage:
                    proxy_writer.append("model_usage", usage, finished.isoformat())
                proxy_writer.append(
                    "proxy_terminal",
                    proxy_terminal,
                    finished.isoformat(),
                )
                proxy_writer.seal()
            verifier_status = (
                "judged" if result_status == "completed" else "not_run"
            )
            _write_json(bundle / "verifier/reward.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION, "trial_id": config.trial_id,
                "lock_sha256": lock_sha256, "status": verifier_status,
                "reward": score,
            })
            _write_json(bundle / "verifier/evidence.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION, "trial_id": config.trial_id,
                "lock_sha256": lock_sha256, "status": verifier_status,
                "checker_exit": checker_exit,
                "reward": score, "task_content_sha256": task_sidecar["task_content_sha256"],
                "final_state_sha256": final_digest,
            })
            completed = result_status == "completed"
            failure_class = (
                "solved"
                if completed and checker_exit == 0
                else "wrong_answer"
                if completed
                else result_status
            )
            _write_json(bundle / "result.json", {
                "schema_version": BUNDLE_SCHEMA_VERSION, "trial_id": config.trial_id,
                "lock_sha256": lock_sha256, "status": result_status,
                "attempts": completed_attempts,
                "retry_count": completed_attempts - 1, "timeout_s": config.timeout_s,
                "started_at": started.isoformat(), "finished_at": finished.isoformat(),
                "agent_started_at": (
                    agent_started_at.isoformat()
                    if agent_started_at is not None
                    else None
                ),
                "agent_finished_at": (
                    agent_finished_at.isoformat()
                    if agent_finished_at is not None
                    else None
                ),
                "timings": {"env_setup_s": setup_s, "agent_s": agent_s, "verifier_s": verifier_s, "total_s": total_s},
                "outcome": {
                    "completed": completed,
                    "score": score,
                    "checker_exit": checker_exit,
                    "error": outcome_error,
                    "failure_class": failure_class,
                    "failure_reason": failure_reason,
                },
                "mcp_event_count": mcp_count,
                "focus_event_count": len(focus_events),
                "process_event_count": len(process_events),
            })
            _write_manifest(bundle, config.trial_id)
            load_native_trial(bundle)
            os.replace(bundle, config.output_dir)
        row = import_native_trial(config.output_dir, config.results_path)
    return NativeRunOutcome(config.output_dir, config.results_path, row, attempts_dir)


def main(argv: list[str] | None = None) -> int:
    from .native_cli import main as native_cli_main

    return native_cli_main(
        argv,
        run_native=run_native,
        run_error=NativeRunError,
    )


if __name__ == "__main__":
    raise SystemExit(main())
