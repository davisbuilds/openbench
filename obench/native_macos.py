"""Safety primitives for native macOS computer-use benchmark runs.

This module deliberately does not orchestrate suites. It provides the
exclusive lease, fail-closed preflight, focus monitoring, and bounded phase
execution needed by a future native runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import tempfile
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


class MacOSAppInspector:
    """Read running application identity/version through AppKit."""

    def inspect(self, requirements: Sequence[AppRequirement]) -> Sequence[AppEvidence]:
        try:
            from AppKit import NSWorkspace
            from Foundation import NSBundle
        except ImportError as exc:  # pragma: no cover - host dependency
            raise PreflightEvidenceError(
                "PyObjC AppKit/Foundation are required for app preflight"
            ) from exc

        required = {item.bundle_identifier for item in requirements}
        evidence = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            bundle_id = app.bundleIdentifier()
            if bundle_id not in required:
                continue
            bundle_url = app.bundleURL()
            path = str(bundle_url.path()) if bundle_url is not None else None
            bundle = NSBundle.bundleWithURL_(bundle_url) if bundle_url is not None else None
            info = bundle.infoDictionary() if bundle is not None else None
            version = None
            if info is not None:
                version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
            evidence.append(
                AppEvidence(
                    bundle_identifier=str(bundle_id),
                    version=str(version) if version is not None else None,
                    running=True,
                    path=path,
                )
            )
        return evidence


class MacOSSessionReader:
    """Read lock state using the same CoreGraphics session dictionary as MCP."""

    _LOCK_KEY = "CGSSessionScreenIsLocked"

    def screen_unlocked(self) -> bool | None:
        try:
            from Quartz import CGSessionCopyCurrentDictionary
        except ImportError as exc:  # pragma: no cover - host dependency
            raise PreflightEvidenceError(
                "PyObjC Quartz is required for unlocked-screen evidence"
            ) from exc
        session = CGSessionCopyCurrentDictionary()
        if session is None or not isinstance(session, Mapping):
            return None
        locked = session.get(self._LOCK_KEY, False)
        if type(locked) is not bool:
            return None
        return not locked


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def run_preflight(
    spec: PreflightSpec,
    *,
    computer_use_binary: str = "computer-use-mcp",
    command_runner: CommandRunner = subprocess.run,
    app_inspector: AppInspector | None = None,
    session_reader: SessionReader | None = None,
    platform_reader: Callable[[], str] = platform.system,
    timeout_s: float = 15.0,
) -> PreflightResult:
    """Collect and evaluate non-mutating native macOS preflight evidence."""

    if timeout_s <= 0:
        raise ValueError("preflight timeout must be positive")
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
    inspector = app_inspector or MacOSAppInspector()
    reader = session_reader or MacOSSessionReader()
    apps = inspector.inspect(spec.required_apps)
    unlocked = reader.screen_unlocked() if spec.require_unlocked_screen else None
    return evaluate_preflight(
        spec,
        platform_name=platform_reader(),
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
        self._lock = threading.Lock()
        self._started = False

    @property
    def violations(self) -> tuple[FocusViolation, ...]:
        with self._lock:
            return tuple(self._violations)

    def _observe(self, event: FocusEvent) -> None:
        if event.bundle_identifier in self.allowed_bundle_identifiers:
            return
        violation = FocusViolation(
            event=event,
            reason=(
                "activated application has no bundle identifier"
                if event.bundle_identifier is None
                else f"bundle identifier {event.bundle_identifier!r} is not allowed"
            ),
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
        self._started = False
        self.event_source.stop()

    def __enter__(self) -> "MacOSFocusMonitor":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.stop()
        return False


class NSWorkspaceActivationEventSource:
    """Real activation source backed by NSWorkspace notifications.

    Delivery relies on the caller's active Cocoa run loop. The source is kept
    separate so a future runner can select its run-loop ownership explicitly.
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic):
        self.monotonic = monotonic
        self._center = None
        self._observer = None

    def start(self, callback: Callable[[FocusEvent], None]) -> None:
        if self._observer is not None:
            raise RuntimeError("activation event source is already started")
        try:
            from AppKit import (
                NSWorkspace,
                NSWorkspaceApplicationKey,
                NSWorkspaceDidActivateApplicationNotification,
            )
        except ImportError as exc:  # pragma: no cover - host dependency
            raise NativeMacOSError(
                "PyObjC AppKit is required for focus monitoring"
            ) from exc

        center = NSWorkspace.sharedWorkspace().notificationCenter()

        def observer(notification):
            app = notification.userInfo().get(NSWorkspaceApplicationKey)
            callback(
                FocusEvent(
                    bundle_identifier=(
                        str(app.bundleIdentifier())
                        if app is not None and app.bundleIdentifier() is not None
                        else None
                    ),
                    application_name=(
                        str(app.localizedName())
                        if app is not None and app.localizedName() is not None
                        else None
                    ),
                    pid=(
                        int(app.processIdentifier())
                        if app is not None
                        else None
                    ),
                    observed_at_monotonic=self.monotonic(),
                )
            )

        token = center.addObserverForName_object_queue_usingBlock_(
            NSWorkspaceDidActivateApplicationNotification, None, None, observer
        )
        self._center = center
        self._observer = token
        frontmost = NSWorkspace.sharedWorkspace().frontmostApplication()
        callback(
            FocusEvent(
                bundle_identifier=(
                    str(frontmost.bundleIdentifier())
                    if frontmost is not None
                    and frontmost.bundleIdentifier() is not None
                    else None
                ),
                application_name=(
                    str(frontmost.localizedName())
                    if frontmost is not None and frontmost.localizedName() is not None
                    else None
                ),
                pid=(
                    int(frontmost.processIdentifier())
                    if frontmost is not None
                    else None
                ),
                observed_at_monotonic=self.monotonic(),
            )
        )

    def stop(self) -> None:
        center, observer = self._center, self._observer
        self._center = None
        self._observer = None
        if center is not None and observer is not None:
            center.removeObserver_(observer)


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

    def _tail(self, stream) -> str:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - self.output_limit_bytes))
        data = stream.read()
        prefix = "[output truncated]\n" if size > self.output_limit_bytes else ""
        return prefix + data.decode("utf-8", "replace")

    @staticmethod
    def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            proc.send_signal(sig)

    def run_phase(self, spec: PhaseSpec) -> PhaseOutcome:
        started_wall = datetime.now(timezone.utc).isoformat()
        started = self.monotonic()
        stdout_file = tempfile.TemporaryFile(mode="w+b")
        stderr_file = tempfile.TemporaryFile(mode="w+b")
        proc = None
        status = PhaseStatus.SPAWN_ERROR
        exit_code = None
        termination = None
        error = None
        try:
            try:
                proc = self.popen(
                    list(spec.argv),
                    cwd=spec.cwd,
                    env=dict(spec.env) if spec.env is not None else None,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                try:
                    exit_code = proc.wait(timeout=spec.timeout_s)
                    status = (
                        PhaseStatus.PASSED
                        if exit_code == 0
                        else PhaseStatus.FAILED
                    )
                except subprocess.TimeoutExpired:
                    status = PhaseStatus.TIMED_OUT
                    termination = "SIGTERM"
                    self._signal_group(proc, signal.SIGTERM)
                    try:
                        proc.wait(timeout=self.terminate_grace_s)
                    except subprocess.TimeoutExpired:
                        termination = "SIGTERM+SIGKILL"
                        self._signal_group(proc, signal.SIGKILL)
                        proc.wait()
                    exit_code = proc.returncode
            return PhaseOutcome(
                name=spec.name,
                status=status,
                argv=spec.argv,
                timeout_s=spec.timeout_s,
                started_at=started_wall,
                duration_s=max(0.0, self.monotonic() - started),
                exit_code=exit_code,
                stdout=self._tail(stdout_file),
                stderr=self._tail(stderr_file),
                termination=termination,
                error=error,
            )
        finally:
            if proc is not None and proc.poll() is None:
                self._signal_group(proc, signal.SIGKILL)
                proc.wait()
            stdout_file.close()
            stderr_file.close()

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

        outcomes = []
        prior_failed = False
        for spec in (setup, agent, verifier):
            if prior_failed:
                outcomes.append(self._skipped(spec, "prior phase did not pass"))
                continue
            outcome = self.run_phase(spec)
            outcomes.append(outcome)
            prior_failed = not outcome.passed
        outcomes.append(self.run_phase(reset))
        return NativePhaseRun(tuple(outcomes))
