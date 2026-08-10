"""Safety primitives for native macOS computer-use benchmark runs.

This module deliberately does not orchestrate suites. It provides the
exclusive lease, fail-closed preflight, focus monitoring, and bounded phase
execution needed by a future native runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import signal
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - this module is macOS/Unix only
    fcntl = None


class NativeMacOSError(RuntimeError):
    """Base error for native macOS execution safety failures."""


class LeaseUnavailableError(NativeMacOSError):
    """Another cooperating process owns the whole-run lease."""

    def __init__(self, path: Path, owner: Mapping[str, Any] | None):
        self.path = path
        self.owner = dict(owner) if owner is not None else None
        detail = json.dumps(self.owner, sort_keys=True) if self.owner else "unreadable"
        super().__init__(f"native macOS run lease is held at {path}; owner={detail}")


class PreflightEvidenceError(NativeMacOSError):
    """Required preflight evidence is malformed, unknown, or unavailable."""


@dataclass(frozen=True)
class LeaseOwner:
    run_id: str
    pid: int
    hostname: str
    started_at: str
    argv: tuple[str, ...]

    @classmethod
    def current(cls, run_id: str | None = None) -> "LeaseOwner":
        return cls(
            run_id=run_id or str(uuid.uuid4()),
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=datetime.now(timezone.utc).isoformat(),
            argv=tuple(os.sys.argv),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state": "held",
            "run_id": self.run_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "argv": list(self.argv),
        }


class WholeRunLease:
    """Exclusive advisory lease held by one open file descriptor.

    ``flock`` is released by the OS if the process exits or crashes. The JSON
    in the lock file is diagnostic only and never determines ownership.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        owner: LeaseOwner | None = None,
        blocking: bool = False,
    ):
        self.path = Path(path).expanduser().resolve()
        self.owner = owner or LeaseOwner.current()
        self.blocking = blocking
        self._fd: int | None = None

    @staticmethod
    def read_owner(path: str | os.PathLike[str]) -> dict[str, Any] | None:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "WholeRunLease":
        if self._fd is not None:
            raise RuntimeError("whole-run lease is already held by this object")
        if fcntl is None:
            raise RuntimeError("native macOS run leases require fcntl")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                raise LeaseUnavailableError(
                    self.path, self.read_owner(self.path)
                ) from exc
            payload = json.dumps(
                self.owner.as_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
            self._fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "WholeRunLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.release()
        return False


@dataclass(frozen=True)
class PermissionEvidence:
    granted: bool
    status: str


@dataclass(frozen=True)
class ComputerUseHealth:
    report_version: int
    version: str
    executable_path: str
    bundle_identifier: str | None
    accessibility: PermissionEvidence
    screen_recording: PermissionEvidence
    capture_status: str


_DAEMON_READY_SUMMARY = (
    "Health report is ready: permissions and capture health are verified."
)


def _daemon_health_ready(
    computer_use_binary: str,
    *,
    timeout_s: float,
) -> bool:
    """Ask an existing app-context daemon for its effective TCC health."""

    socket_path = Path.home() / "Library/Caches/computer-use-mcp/daemon.sock"
    if not socket_path.exists() or socket_path.is_symlink():
        return False
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [computer_use_binary, "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            return False

        def exchange(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
            process.stdin.write(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            )
            process.stdin.flush()
            readable, _, _ = select.select([process.stdout], [], [], timeout_s)
            if not readable:
                return None
            value = json.loads(process.stdout.readline())
            return value if isinstance(value, dict) else None

        initialized = exchange({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "openbench-preflight", "version": "1"},
            },
        })
        if not initialized or initialized.get("id") != 1:
            return False
        process.stdin.write(
            '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        )
        process.stdin.flush()
        response = exchange({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "health_report",
                "arguments": {"probe_capture_service": True},
            },
        })
        if not response or response.get("id") != 2:
            return False
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError") is True:
            return False
        content = result.get("content")
        return (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and content[0].get("text") == _DAEMON_READY_SUMMARY
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return False
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PreflightEvidenceError(f"{field_name} must be a JSON object")
    return value


def _string(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PreflightEvidenceError(f"{field_name} must be a non-empty string")
    return value


def _permission(value: Any, field_name: str) -> PermissionEvidence:
    item = _object(value, field_name)
    granted = item.get("granted")
    status = item.get("status")
    if type(granted) is not bool:
        raise PreflightEvidenceError(f"{field_name}.granted must be a boolean")
    if status not in {"granted", "not_granted"}:
        raise PreflightEvidenceError(f"{field_name}.status is unknown: {status!r}")
    if granted != (status == "granted"):
        raise PreflightEvidenceError(
            f"{field_name} has inconsistent granted/status evidence"
        )
    return PermissionEvidence(granted=granted, status=status)


def parse_health_report_json(value: str | bytes | Mapping[str, Any]) -> ComputerUseHealth:
    """Parse the source-proven computer-use-mcp health-report v1 schema."""

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PreflightEvidenceError("health report is not UTF-8") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PreflightEvidenceError("health report is not valid JSON") from exc
    report = _object(value, "health report")
    report_version = report.get("reportVersion")
    if type(report_version) is not int or report_version != 1:
        raise PreflightEvidenceError(
            f"unsupported health report version: {report_version!r}"
        )
    permissions = _object(report.get("permissions"), "permissions")
    capture = _object(report.get("captureService"), "captureService")
    capture_status = capture.get("status")
    if capture_status not in {"responsive", "not_responding", "skipped"}:
        raise PreflightEvidenceError(
            f"captureService.status is unknown: {capture_status!r}"
        )
    bundle_identifier = _string(
        report.get("bundleIdentifier"), "bundleIdentifier", optional=True
    )
    return ComputerUseHealth(
        report_version=1,
        version=_string(report.get("version"), "version"),  # type: ignore[arg-type]
        executable_path=_string(
            report.get("executablePath"), "executablePath"
        ),  # type: ignore[arg-type]
        bundle_identifier=bundle_identifier,
        accessibility=_permission(
            permissions.get("accessibility"), "permissions.accessibility"
        ),
        screen_recording=_permission(
            permissions.get("screenRecording"), "permissions.screenRecording"
        ),
        capture_status=capture_status,
    )


@dataclass(frozen=True)
class AppRequirement:
    bundle_identifier: str
    version: str

    def __post_init__(self) -> None:
        if not self.bundle_identifier or not self.version:
            raise ValueError("app bundle identifier and version must be non-empty")


@dataclass(frozen=True)
class AppEvidence:
    bundle_identifier: str
    version: str | None
    running: bool
    path: str | None = None


@dataclass(frozen=True)
class PreflightSpec:
    required_apps: tuple[AppRequirement, ...] = ()
    computer_use_version: str | None = None
    computer_use_bundle_identifier: str | None = None
    require_unlocked_screen: bool = True


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    observed: Any
    required: Any
    source: str


@dataclass(frozen=True)
class PreflightResult:
    checks: tuple[PreflightCheck, ...]
    health: ComputerUseHealth | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def require_passed(self) -> "PreflightResult":
        if not self.passed:
            names = ", ".join(check.name for check in self.failures)
            raise PreflightEvidenceError(f"native macOS preflight failed: {names}")
        return self


def evaluate_preflight(
    spec: PreflightSpec,
    *,
    platform_name: str,
    health: ComputerUseHealth,
    apps: Sequence[AppEvidence],
    screen_unlocked: bool | None,
) -> PreflightResult:
    """Evaluate collected evidence without mutating the host."""

    checks = [
        PreflightCheck("platform", platform_name == "Darwin", platform_name, "Darwin", "platform.system"),
        PreflightCheck(
            "accessibility",
            health.accessibility.granted,
            health.accessibility.status,
            "granted",
            "computer-use-mcp health_report",
        ),
        PreflightCheck(
            "screen_recording",
            health.screen_recording.granted,
            health.screen_recording.status,
            "granted",
            "computer-use-mcp health_report",
        ),
        PreflightCheck(
            "capture_health",
            health.capture_status == "responsive",
            health.capture_status,
            "responsive",
            "computer-use-mcp health_report --probe-capture",
        ),
    ]
    if spec.computer_use_version is not None:
        checks.append(
            PreflightCheck(
                "computer_use_version",
                health.version == spec.computer_use_version,
                health.version,
                spec.computer_use_version,
                "computer-use-mcp health_report",
            )
        )
    if spec.computer_use_bundle_identifier is not None:
        checks.append(
            PreflightCheck(
                "computer_use_bundle_identifier",
                health.bundle_identifier == spec.computer_use_bundle_identifier,
                health.bundle_identifier,
                spec.computer_use_bundle_identifier,
                "computer-use-mcp health_report",
            )
        )
    if spec.require_unlocked_screen:
        checks.append(
            PreflightCheck(
                "screen_unlocked",
                screen_unlocked is True,
                screen_unlocked,
                True,
                "CGSessionCopyCurrentDictionary",
            )
        )

    by_bundle: dict[str, list[AppEvidence]] = {}
    for app in apps:
        by_bundle.setdefault(app.bundle_identifier, []).append(app)
    for required in spec.required_apps:
        matches = by_bundle.get(required.bundle_identifier, [])
        exact = [
            app for app in matches
            if app.running and app.version == required.version
        ]
        observed = [
            {"version": app.version, "running": app.running, "path": app.path}
            for app in matches
        ]
        checks.append(
            PreflightCheck(
                f"app:{required.bundle_identifier}",
                len(exact) == 1,
                observed,
                {"version": required.version, "running": True, "matches": 1},
                "NSWorkspace.runningApplications + NSBundle",
            )
        )
    return PreflightResult(checks=tuple(checks), health=health)


class AppInspector(Protocol):
    def inspect(self, requirements: Sequence[AppRequirement]) -> Sequence[AppEvidence]:
        """Return running-app evidence for the requested bundle identities."""


class SessionReader(Protocol):
    def screen_unlocked(self) -> bool | None:
        """Return True/False only when lock state is source-proven."""


NATIVE_HELPER_PROTOCOL_VERSION = 2
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _parse_helper_reply(
    value: str | bytes | Mapping[str, Any],
    *,
    expected_kind: str,
) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PreflightEvidenceError("native helper output is not UTF-8") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PreflightEvidenceError(
                "native helper output is not valid JSON"
            ) from exc
    reply = _object(value, "native helper reply")
    version = reply.get("protocolVersion")
    if type(version) is not int or version != NATIVE_HELPER_PROTOCOL_VERSION:
        raise PreflightEvidenceError(
            f"unsupported native helper protocol version: {version!r}"
        )
    if reply.get("kind") != expected_kind:
        raise PreflightEvidenceError(
            f"native helper returned kind {reply.get('kind')!r}; "
            f"expected {expected_kind!r}"
        )
    return reply


class NativeMacOSHelperResolver:
    """Resolve or compile the checked-in Swift helper, then probe its protocol."""

    def __init__(
        self,
        *,
        source_path: str | os.PathLike[str] | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        prebuilt_path: str | os.PathLike[str] | None = None,
        command_runner: CommandRunner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        platform_reader: Callable[[], str] = platform.system,
        machine_reader: Callable[[], str] = platform.machine,
        build_timeout_s: float = 120.0,
        probe_timeout_s: float = 5.0,
    ):
        self.source_path = Path(
            source_path or Path(__file__).with_name("native_macos_helper.swift")
        ).expanduser().resolve()
        self.cache_dir = Path(
            cache_dir
            or Path("~/Library/Caches/OpenBench/native-macos-helper").expanduser()
        ).expanduser().resolve()
        self.prebuilt_path = (
            Path(prebuilt_path).expanduser().resolve()
            if prebuilt_path is not None
            else None
        )
        self.command_runner = command_runner
        self.which = which
        self.platform_reader = platform_reader
        self.machine_reader = machine_reader
        self.build_timeout_s = build_timeout_s
        self.probe_timeout_s = probe_timeout_s

    def _run(self, argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return self.command_runner(
                list(argv),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreflightEvidenceError(
                f"native helper command failed ({argv[0]}): {exc}"
            ) from exc

    def _probe(self, path: Path) -> None:
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PreflightEvidenceError(
                f"native macOS helper is missing or not executable: {path}"
            )
        completed = self._run([str(path), "protocol"], timeout=self.probe_timeout_s)
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()[-1000:]
            raise PreflightEvidenceError(
                f"native macOS helper protocol probe exited "
                f"{completed.returncode}: {detail}"
            )
        _parse_helper_reply(completed.stdout, expected_kind="protocol")

    def resolve(self) -> Path:
        if self.platform_reader() != "Darwin":
            raise PreflightEvidenceError(
                "native macOS helper requires platform.system() == 'Darwin'"
            )
        if self.prebuilt_path is not None:
            self._probe(self.prebuilt_path)
            return self.prebuilt_path
        try:
            source = self.source_path.read_bytes()
        except OSError as exc:
            raise PreflightEvidenceError(
                f"native macOS helper source is unavailable: {self.source_path}"
            ) from exc
        digest = hashlib.sha256(
            source + b"\0" + self.machine_reader().encode("utf-8")
        ).hexdigest()
        target_dir = self.cache_dir / digest
        target = target_dir / "native-macos-helper"
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(target_dir, 0o700)
        with WholeRunLease(target_dir / ".build.lock", blocking=True):
            had_cached_target = target.is_file()
            rebuild = not had_cached_target
            if not rebuild:
                try:
                    self._probe(target)
                    return target
                except PreflightEvidenceError:
                    rebuild = True
            if rebuild:
                swiftc = self.which("swiftc")
                if swiftc is None:
                    reason = (
                        "cached native macOS helper is invalid, and rebuilding"
                        if had_cached_target
                        else "building the native macOS helper"
                    )
                    raise PreflightEvidenceError(
                        f"{reason} requires swiftc from the Xcode Command Line "
                        "Tools; no compiler was found on PATH"
                    )
                temporary = target_dir / f".native-macos-helper-{uuid.uuid4().hex}"
                try:
                    completed = self._run(
                        [
                            swiftc,
                            str(self.source_path),
                            "-O",
                            "-o",
                            str(temporary),
                        ],
                        timeout=self.build_timeout_s,
                    )
                    if completed.returncode != 0:
                        detail = (completed.stderr or "").strip()[-4000:]
                        raise PreflightEvidenceError(
                            "native macOS helper compilation failed "
                            f"(swiftc exit {completed.returncode}): {detail}"
                        )
                    if not temporary.is_file():
                        raise PreflightEvidenceError(
                            "swiftc reported success but produced no native "
                            f"macOS helper at {temporary}"
                        )
                    os.chmod(temporary, 0o755)
                    os.replace(temporary, target)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            self._probe(target)
        return target


def _run_native_helper(
    helper_path: Path,
    command: Sequence[str],
    *,
    command_runner: CommandRunner,
    timeout_s: float,
    expected_kind: str,
) -> Mapping[str, Any]:
    try:
        completed = command_runner(
            [str(helper_path), *command],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightEvidenceError(
            f"native helper {command[0]} command failed: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-1000:]
        raise PreflightEvidenceError(
            f"native helper {command[0]} exited {completed.returncode}: {detail}"
        )
    return _parse_helper_reply(completed.stdout, expected_kind=expected_kind)


class MacOSAppInspector:
    """Read running app identity/version through the native Swift helper."""

    def __init__(
        self,
        helper_path: str | os.PathLike[str],
        *,
        command_runner: CommandRunner = subprocess.run,
        timeout_s: float = 5.0,
    ):
        self.helper_path = Path(helper_path)
        self.command_runner = command_runner
        self.timeout_s = timeout_s

    def inspect(self, requirements: Sequence[AppRequirement]) -> Sequence[AppEvidence]:
        bundle_ids = sorted({item.bundle_identifier for item in requirements})
        reply = _run_native_helper(
            self.helper_path,
            ["apps", *bundle_ids],
            command_runner=self.command_runner,
            timeout_s=self.timeout_s,
            expected_kind="apps",
        )
        raw_apps = reply.get("apps")
        if not isinstance(raw_apps, list):
            raise PreflightEvidenceError("native helper apps must be a JSON array")
        evidence = []
        for index, raw in enumerate(raw_apps):
            app = _object(raw, f"native helper apps[{index}]")
            bundle_identifier = _string(
                app.get("bundleIdentifier"),
                f"native helper apps[{index}].bundleIdentifier",
            )
            version = _string(
                app.get("version"),
                f"native helper apps[{index}].version",
                optional=True,
            )
            path = _string(
                app.get("path"),
                f"native helper apps[{index}].path",
                optional=True,
            )
            running = app.get("running")
            if type(running) is not bool:
                raise PreflightEvidenceError(
                    f"native helper apps[{index}].running must be a boolean"
                )
            if bundle_identifier not in bundle_ids:
                raise PreflightEvidenceError(
                    "native helper returned an undeclared bundle identifier: "
                    f"{bundle_identifier!r}"
                )
            evidence.append(
                AppEvidence(bundle_identifier, version, running, path)  # type: ignore[arg-type]
            )
        return evidence


class MacOSSessionReader:
    """Read CGSession lock evidence through the native Swift helper."""

    def __init__(
        self,
        helper_path: str | os.PathLike[str],
        *,
        command_runner: CommandRunner = subprocess.run,
        timeout_s: float = 5.0,
    ):
        self.helper_path = Path(helper_path)
        self.command_runner = command_runner
        self.timeout_s = timeout_s

    def screen_unlocked(self) -> bool | None:
        reply = _run_native_helper(
            self.helper_path,
            ["session"],
            command_runner=self.command_runner,
            timeout_s=self.timeout_s,
            expected_kind="session",
        )
        observed_at = reply.get("observedAt")
        observed_monotonic_ns = reply.get("observedAtMonotonicNs")
        sequence = reply.get("sequence")
        if not isinstance(observed_at, str):
            raise PreflightEvidenceError(
                "native helper session observedAt must be an ISO-8601 string"
            )
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PreflightEvidenceError(
                "native helper session observedAt is not ISO-8601"
            ) from exc
        if parsed.tzinfo is None:
            raise PreflightEvidenceError(
                "native helper session observedAt must include an offset"
            )
        if (
            type(observed_monotonic_ns) is not int
            or observed_monotonic_ns < 0
            or type(sequence) is not int
            or sequence != 1
        ):
            raise PreflightEvidenceError(
                "native helper session source sequence/timestamp is invalid"
            )
        if reply.get("status") == "unknown":
            if reply.get("screenUnlocked") is not None:
                raise PreflightEvidenceError(
                    "native helper unknown session cannot assert screenUnlocked"
                )
            return None
        if reply.get("status") != "known":
            raise PreflightEvidenceError(
                f"native helper session status is unknown: {reply.get('status')!r}"
            )
        unlocked = reply.get("screenUnlocked")
        if type(unlocked) is not bool:
            raise PreflightEvidenceError(
                "native helper known session requires boolean screenUnlocked"
            )
        return unlocked


def run_preflight(
    spec: PreflightSpec,
    *,
    computer_use_binary: str = "computer-use-mcp",
    command_runner: CommandRunner = subprocess.run,
    app_inspector: AppInspector | None = None,
    session_reader: SessionReader | None = None,
    helper_resolver: NativeMacOSHelperResolver | None = None,
    platform_reader: Callable[[], str] = platform.system,
    daemon_health_probe: Callable[[str, float], bool] | None = None,
    timeout_s: float = 15.0,
) -> PreflightResult:
    """Collect and evaluate non-mutating native macOS preflight evidence."""

    if timeout_s <= 0:
        raise ValueError("preflight timeout must be positive")
    platform_name = platform_reader()
    if platform_name != "Darwin":
        raise PreflightEvidenceError(
            f"native macOS preflight requires Darwin, observed {platform_name!r}"
        )

    helper_path = None
    needs_helper = (
        (app_inspector is None and bool(spec.required_apps))
        or (session_reader is None and spec.require_unlocked_screen)
    )
    if needs_helper:
        resolver = helper_resolver or NativeMacOSHelperResolver(
            command_runner=command_runner,
            platform_reader=lambda: platform_name,
        )
        helper_path = resolver.resolve()
    try:
        completed = command_runner(
            [computer_use_binary, "health_report", "--json", "--probe-capture"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightEvidenceError(f"health report command failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-1000:]
        raise PreflightEvidenceError(
            f"health report exited {completed.returncode}: {detail}"
        )
    health = parse_health_report_json(completed.stdout)
    probe_daemon = daemon_health_probe or (
        lambda binary, timeout: _daemon_health_ready(binary, timeout_s=timeout)
    )
    if (
        not health.accessibility.granted
        or not health.screen_recording.granted
        or health.capture_status != "responsive"
    ) and probe_daemon(computer_use_binary, timeout_s):
        health = ComputerUseHealth(
            report_version=health.report_version,
            version=health.version,
            executable_path=health.executable_path,
            bundle_identifier=health.bundle_identifier,
            accessibility=PermissionEvidence(granted=True, status="granted"),
            screen_recording=PermissionEvidence(granted=True, status="granted"),
            capture_status="responsive",
        )
    if spec.required_apps:
        inspector = app_inspector or MacOSAppInspector(
            helper_path, command_runner=command_runner  # type: ignore[arg-type]
        )
        apps = inspector.inspect(spec.required_apps)
    else:
        apps = ()
    if spec.require_unlocked_screen:
        reader = session_reader or MacOSSessionReader(
            helper_path, command_runner=command_runner  # type: ignore[arg-type]
        )
        unlocked = reader.screen_unlocked()
    else:
        unlocked = None
    return evaluate_preflight(
        spec,
        platform_name=platform_name,
        health=health,
        apps=apps,
        screen_unlocked=unlocked,
    )


@dataclass(frozen=True)
class FocusEvent:
    bundle_identifier: str | None
    application_name: str | None
    pid: int | None
    observed_at_monotonic: float
    observed_at: str | None = None
    source_sequence: int | None = None
    source_monotonic_ns: int | None = None
    sample_kind: str | None = None
    session_status: str | None = None
    screen_unlocked: bool | None = None


@dataclass(frozen=True)
class FocusViolation:
    event: FocusEvent
    reason: str


class ActivationEventSource(Protocol):
    def start(self, callback: Callable[[FocusEvent], None]) -> None:
        """Begin delivering activation events."""

    def stop(self) -> None:
        """Stop delivering activation events."""


class FocusMonitor(Protocol):
    @property
    def violations(self) -> tuple[FocusViolation, ...]:
        """Observed activations outside the allowed bundle identities."""

    def start(self) -> None:
        """Start monitoring."""

    def stop(self) -> None:
        """Stop monitoring."""


class MacOSFocusMonitor:
    """Fail-closed focus policy over NSWorkspace activation notifications."""

    def __init__(
        self,
        allowed_bundle_identifiers: Sequence[str],
        *,
        event_source: ActivationEventSource | None = None,
        on_violation: Callable[[FocusViolation], None] | None = None,
    ):
        if not allowed_bundle_identifiers:
            raise ValueError("at least one allowed bundle identifier is required")
        self.allowed_bundle_identifiers = frozenset(allowed_bundle_identifiers)
        self.event_source = event_source or NSWorkspaceActivationEventSource()
        self.on_violation = on_violation
        self._violations: list[FocusViolation] = []
        self._events: list[FocusEvent] = []
        self._lock = threading.Lock()
        self._started = False

    @property
    def violations(self) -> tuple[FocusViolation, ...]:
        with self._lock:
            return tuple(self._violations)

    @property
    def events(self) -> tuple[FocusEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def error(self) -> BaseException | None:
        return getattr(self.event_source, "error", None)

    def require_healthy(self) -> None:
        if self.error is not None:
            raise NativeMacOSError(
                f"native focus monitor failed: {self.error}"
            ) from self.error

    def _observe(self, event: FocusEvent) -> None:
        with self._lock:
            self._events.append(event)
        if (
            event.session_status in (None, "known")
            and event.screen_unlocked in (None, True)
            and event.bundle_identifier in self.allowed_bundle_identifiers
        ):
            return
        if event.session_status not in (None, "known"):
            reason = "screen lock state is not source-proven"
        elif event.screen_unlocked is False:
            reason = "screen became locked during native monitoring"
        elif event.screen_unlocked is None and event.session_status is not None:
            reason = "screen lock state is unavailable"
        else:
            reason = (
                "activated application has no bundle identifier"
                if event.bundle_identifier is None
                else f"bundle identifier {event.bundle_identifier!r} is not allowed"
            )
        violation = FocusViolation(
            event=event,
            reason=reason,
        )
        with self._lock:
            self._violations.append(violation)
        if self.on_violation is not None:
            self.on_violation(violation)

    def start(self) -> None:
        if self._started:
            raise RuntimeError("focus monitor is already started")
        self._started = True
        try:
            self.event_source.start(self._observe)
        except BaseException:
            self._started = False
            self.event_source.stop()
            raise

    def stop(self) -> None:
        if not self._started:
            return
        self.event_source.stop()
        self._started = False
        self.require_healthy()

    def __enter__(self) -> "MacOSFocusMonitor":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.stop()
        except BaseException as monitor_error:
            if exc is None:
                raise
            setattr(exc, "focus_monitor_error", monitor_error)
        return False


class NSWorkspaceActivationEventSource:
    """NSWorkspace activation events streamed by the native Swift helper."""

    def __init__(
        self,
        *,
        helper_resolver: NativeMacOSHelperResolver | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        startup_timeout_s: float = 5.0,
    ):
        self.helper_resolver = helper_resolver or NativeMacOSHelperResolver()
        self.popen = popen
        self.monotonic = monotonic
        self.startup_timeout_s = startup_timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_capture: _BoundedCapture | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._error: BaseException | None = None
        self._error_lock = threading.Lock()
        self._last_sequence = 0
        self._last_source_monotonic_ns = 0
        self._terminal_seen = False

    @property
    def error(self) -> BaseException | None:
        with self._error_lock:
            return self._error

    def _set_error(self, error: BaseException) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = error
        self._ready.set()

    def _parse_focus_event(self, line: bytes) -> FocusEvent:
        reply = _parse_helper_reply(line, expected_kind="focus")
        bundle_identifier = reply.get("bundleIdentifier")
        application_name = reply.get("applicationName")
        pid = reply.get("pid")
        observed_at = reply.get("observedAt")
        source_monotonic_ns = reply.get("observedAtMonotonicNs")
        source_sequence = reply.get("sequence")
        sample_kind = reply.get("sampleKind")
        session_status = reply.get("sessionStatus")
        screen_unlocked = reply.get("screenUnlocked")
        if bundle_identifier is not None and not isinstance(bundle_identifier, str):
            raise PreflightEvidenceError(
                "native helper focus bundleIdentifier must be a string or null"
            )
        if application_name is not None and not isinstance(application_name, str):
            raise PreflightEvidenceError(
                "native helper focus applicationName must be a string or null"
            )
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise PreflightEvidenceError(
                "native helper focus pid must be a positive integer or null"
            )
        if not isinstance(observed_at, str):
            raise PreflightEvidenceError(
                "native helper focus observedAt must be an ISO-8601 string"
            )
        try:
            parsed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PreflightEvidenceError(
                "native helper focus observedAt is not ISO-8601"
            ) from exc
        if parsed_at.tzinfo is None:
            raise PreflightEvidenceError(
                "native helper focus observedAt must include an offset"
            )
        if (
            type(source_monotonic_ns) is not int
            or source_monotonic_ns < 0
            or type(source_sequence) is not int
            or source_sequence < 1
        ):
            raise PreflightEvidenceError(
                "native helper focus source sequence/timestamp is invalid"
            )
        if sample_kind not in {"baseline", "activation", "heartbeat", "terminal"}:
            raise PreflightEvidenceError(
                f"native helper focus sample kind is invalid: {sample_kind!r}"
            )
        if session_status not in {"known", "unknown"}:
            raise PreflightEvidenceError(
                f"native helper focus session status is invalid: {session_status!r}"
            )
        if screen_unlocked is not None and type(screen_unlocked) is not bool:
            raise PreflightEvidenceError(
                "native helper focus screenUnlocked must be boolean or null"
            )
        if session_status == "known" and type(screen_unlocked) is not bool:
            raise PreflightEvidenceError(
                "native helper known focus session requires screenUnlocked"
            )
        if session_status == "unknown" and screen_unlocked is not None:
            raise PreflightEvidenceError(
                "native helper unknown focus session cannot assert screenUnlocked"
            )
        return FocusEvent(
            bundle_identifier=bundle_identifier,
            application_name=application_name,
            pid=pid,
            observed_at_monotonic=self.monotonic(),
            observed_at=parsed_at.isoformat(),
            source_sequence=source_sequence,
            source_monotonic_ns=source_monotonic_ns,
            sample_kind=sample_kind,
            session_status=session_status,
            screen_unlocked=screen_unlocked,
        )

    def _read_events(self, callback: Callable[[FocusEvent], None]) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._set_error(NativeMacOSError("native focus helper has no stdout"))
            return
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    if not self._stopping.is_set():
                        self._set_error(
                            NativeMacOSError(
                                f"native focus helper exited {process.poll()}"
                            )
                        )
                    return
                event = self._parse_focus_event(line)
                if (
                    event.source_sequence != self._last_sequence + 1
                    or event.source_monotonic_ns is None
                    or event.source_monotonic_ns <= self._last_source_monotonic_ns
                ):
                    raise PreflightEvidenceError(
                        "native helper focus sequence has a gap or non-monotonic timestamp"
                    )
                if self._last_sequence == 0 and event.sample_kind != "baseline":
                    raise PreflightEvidenceError(
                        "native helper focus stream does not start with a baseline"
                    )
                if self._terminal_seen:
                    raise PreflightEvidenceError(
                        "native helper focus stream contains samples after terminal evidence"
                    )
                self._last_sequence = event.source_sequence
                self._last_source_monotonic_ns = event.source_monotonic_ns
                if event.sample_kind == "terminal":
                    self._terminal_seen = True
                callback(event)
                self._ready.set()
        except BaseException as exc:
            if not self._stopping.is_set():
                self._set_error(exc)
            if process.poll() is None:
                process.terminate()

    def start(self, callback: Callable[[FocusEvent], None]) -> None:
        if self._process is not None:
            raise RuntimeError("activation event source is already started")
        helper_path = self.helper_resolver.resolve()
        self._ready.clear()
        self._stopping.clear()
        with self._error_lock:
            self._error = None
        self._last_sequence = 0
        self._last_source_monotonic_ns = 0
        self._terminal_seen = False
        try:
            process = self.popen(
                [str(helper_path), "focus"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise NativeMacOSError(
                f"failed to start native focus helper: {exc}"
            ) from exc
        self._process = process
        if process.stderr is None:
            self.stop()
            raise NativeMacOSError("native focus helper has no stderr")
        self._stderr_capture = _BoundedCapture(process.stderr, 16 * 1024)
        self._stderr_capture.start()
        self._reader_thread = threading.Thread(
            target=self._read_events,
            args=(callback,),
            daemon=True,
        )
        self._reader_thread.start()
        if not self._ready.wait(timeout=self.startup_timeout_s):
            self._set_error(
                NativeMacOSError(
                    "native focus helper produced no baseline activation event "
                    f"within {self.startup_timeout_s:g}s"
                )
            )
        if self.error is not None:
            error = self.error
            self.stop()
            raise NativeMacOSError(
                f"native focus helper startup failed: {error}"
            ) from error

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._stopping.set()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1)
            if self._reader_thread.is_alive():
                raise NativeMacOSError(
                    "native focus helper reader did not stop; restart is blocked"
                )
        if self._stderr_capture is not None:
            self._stderr_capture.finish(1)
        if self.error is None and not self._terminal_seen:
            self._set_error(
                NativeMacOSError(
                    "native focus helper stopped without terminal health evidence"
                )
            )
        self._process = None
        self._reader_thread = None
        self._stderr_capture = None


class PhaseName(str, Enum):
    SETUP = "setup"
    AGENT = "agent"
    VERIFIER = "verifier"
    RESET = "reset"


class PhaseStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SPAWN_ERROR = "spawn_error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PhaseSpec:
    name: PhaseName
    argv: tuple[str, ...]
    timeout_s: float
    cwd: str | None = None
    env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError(f"{self.name.value} phase command must not be empty")
        if self.timeout_s <= 0:
            raise ValueError(f"{self.name.value} phase timeout must be positive")


@dataclass(frozen=True)
class PhaseOutcome:
    name: PhaseName
    status: PhaseStatus
    argv: tuple[str, ...]
    timeout_s: float
    started_at: str | None
    duration_s: float
    exit_code: int | None
    stdout: str
    stderr: str
    termination: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == PhaseStatus.PASSED


@dataclass(frozen=True)
class NativePhaseRun:
    outcomes: tuple[PhaseOutcome, ...]

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and all(
            outcome.passed
            for outcome in self.outcomes
            if outcome.status != PhaseStatus.SKIPPED
        )

    def outcome(self, name: PhaseName) -> PhaseOutcome:
        return next(item for item in self.outcomes if item.name == name)


class _BoundedCapture:
    """Continuously drain one pipe while retaining only a bounded tail."""

    def __init__(self, stream, limit: int):
        self.stream = stream
        self.limit = limit
        self.total = 0
        self.tail = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    return
                self.total += len(chunk)
                self.tail.extend(chunk)
                if len(self.tail) > self.limit:
                    del self.tail[:-self.limit]
        except (OSError, ValueError):
            return

    def start(self) -> None:
        self.thread.start()

    def finish(self, timeout_s: float) -> str:
        self.thread.join(timeout=max(0.1, timeout_s))
        if self.thread.is_alive():
            self.stream.close()
            self.thread.join(timeout=0.5)
        else:
            self.stream.close()
        prefix = "[output truncated]\n" if self.total > self.limit else ""
        return prefix + bytes(self.tail).decode("utf-8", "replace")


class SubprocessPhaseRunner:
    """Run bounded phases and contain each command in its own process group."""

    def __init__(
        self,
        *,
        terminate_grace_s: float = 2.0,
        output_limit_bytes: int = 64 * 1024,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if terminate_grace_s < 0:
            raise ValueError("terminate grace must not be negative")
        if output_limit_bytes <= 0:
            raise ValueError("output limit must be positive")
        self.terminate_grace_s = terminate_grace_s
        self.output_limit_bytes = output_limit_bytes
        self.popen = popen
        self.monotonic = monotonic

    @staticmethod
    def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            proc.send_signal(sig)

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _cleanup_process_group(self, proc: subprocess.Popen[bytes]) -> str | None:
        """Terminate remaining group members even if the leader already exited."""

        actions = []
        if self._group_alive(proc.pid):
            self._signal_group(proc, signal.SIGTERM)
            actions.append("SIGTERM")
            deadline = self.monotonic() + self.terminate_grace_s
            while self._group_alive(proc.pid) and self.monotonic() < deadline:
                time.sleep(min(0.01, max(0.0, deadline - self.monotonic())))
            if self._group_alive(proc.pid):
                self._signal_group(proc, signal.SIGKILL)
                actions.append("SIGKILL")
        if proc.poll() is None:
            try:
                proc.wait(timeout=max(0.1, self.terminate_grace_s))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                if "SIGKILL" not in actions:
                    actions.append("SIGKILL")
        return "+".join(actions) or None

    def run_phase(self, spec: PhaseSpec) -> PhaseOutcome:
        started_wall = datetime.now(timezone.utc).isoformat()
        started = self.monotonic()
        proc = None
        stdout_capture = None
        stderr_capture = None
        status = PhaseStatus.SPAWN_ERROR
        exit_code = None
        termination = None
        error = None
        stdout = ""
        stderr = ""
        try:
            try:
                proc = self.popen(
                    list(spec.argv),
                    cwd=spec.cwd,
                    env=dict(spec.env) if spec.env is not None else None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if proc.stdout is None or proc.stderr is None:
                    raise RuntimeError("phase process did not expose output pipes")
                stdout_capture = _BoundedCapture(
                    proc.stdout, self.output_limit_bytes
                )
                stderr_capture = _BoundedCapture(
                    proc.stderr, self.output_limit_bytes
                )
                stdout_capture.start()
                stderr_capture.start()
                try:
                    exit_code = proc.wait(timeout=spec.timeout_s)
                    status = (
                        PhaseStatus.PASSED
                        if exit_code == 0
                        else PhaseStatus.FAILED
                    )
                except subprocess.TimeoutExpired:
                    status = PhaseStatus.TIMED_OUT
        finally:
            if proc is not None:
                termination = self._cleanup_process_group(proc)
                exit_code = proc.returncode
            if stdout_capture is not None:
                stdout = stdout_capture.finish(self.terminate_grace_s)
            if stderr_capture is not None:
                stderr = stderr_capture.finish(self.terminate_grace_s)
        return PhaseOutcome(
            name=spec.name,
            status=status,
            argv=spec.argv,
            timeout_s=spec.timeout_s,
            started_at=started_wall,
            duration_s=max(0.0, self.monotonic() - started),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            termination=termination,
            error=error,
        )

    @staticmethod
    def _skipped(spec: PhaseSpec, reason: str) -> PhaseOutcome:
        return PhaseOutcome(
            name=spec.name,
            status=PhaseStatus.SKIPPED,
            argv=spec.argv,
            timeout_s=spec.timeout_s,
            started_at=None,
            duration_s=0.0,
            exit_code=None,
            stdout="",
            stderr="",
            error=reason,
        )

    def run(
        self,
        *,
        setup: PhaseSpec,
        agent: PhaseSpec,
        verifier: PhaseSpec,
        reset: PhaseSpec,
    ) -> NativePhaseRun:
        expected = (
            (setup, PhaseName.SETUP),
            (agent, PhaseName.AGENT),
            (verifier, PhaseName.VERIFIER),
            (reset, PhaseName.RESET),
        )
        for spec, name in expected:
            if spec.name != name:
                raise ValueError(
                    f"expected {name.value} phase, got {spec.name.value}"
                )

        outcomes: list[PhaseOutcome] = []
        prior_failed = False
        interrupted: BaseException | None = None
        interrupted_traceback = None
        try:
            for spec in (setup, agent, verifier):
                if prior_failed:
                    outcomes.append(self._skipped(spec, "prior phase did not pass"))
                    continue
                outcome = self.run_phase(spec)
                outcomes.append(outcome)
                prior_failed = not outcome.passed
        except BaseException as exc:
            interrupted = exc
            interrupted_traceback = exc.__traceback__
        try:
            outcomes.append(self.run_phase(reset))
        except BaseException as reset_exc:
            if interrupted is None:
                interrupted = reset_exc
                interrupted_traceback = reset_exc.__traceback__
            else:
                setattr(interrupted, "reset_error", reset_exc)
        result = NativePhaseRun(tuple(outcomes))
        if interrupted is not None:
            setattr(interrupted, "native_phase_run", result)
            raise interrupted.with_traceback(interrupted_traceback)
        return NativePhaseRun(tuple(outcomes))
