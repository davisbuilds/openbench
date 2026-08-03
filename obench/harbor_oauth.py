"""Host-side lifecycle for an optional Harbor Codex OAuth credential.

This module is stdlib-only. It does not import Harbor; the optional agent class
under :mod:`obench.harbor_agents.codex` performs that import lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

from . import auth_persist

try:
    import fcntl
except ImportError:  # pragma: no cover - OpenBench runners are Unix hosts
    fcntl = None


CODEX_AUTH_JSON_PATH = "CODEX_AUTH_JSON_PATH"
CODEX_AUTH_RETURN_PATH = "OPENBENCH_CODEX_AUTH_RETURN_PATH"
AGENT_IMPORT_PATH = "obench.harbor_agents.codex:OpenBenchCodexOAuth"


class HarborOAuthError(RuntimeError):
    """Base class for fail-closed Harbor OAuth bridge errors."""


class HarborOAuthSetupError(HarborOAuthError):
    """The optional Harbor dependency or bridge configuration is unavailable."""


class ConcurrentCredentialUseError(HarborOAuthError):
    """Another Harbor trial already owns this credential."""


class StaleCredentialError(HarborOAuthError):
    """The credential changed after this trial staged its input generation."""


class MissingAuthReturnError(HarborOAuthError):
    """The Harbor agent did not return its disposable auth.json."""


class HarborOAuthUnsupportedError(HarborOAuthError):
    """The installed Harbor Codex lifecycle lacks the required cleanup boundary."""


class HarborOAuthCaptureError(HarborOAuthError):
    """Harbor could not return auth.json before deleting its remote secret."""


@dataclass(frozen=True)
class HarborOAuthConfig:
    """Values needed to configure exactly one Harbor OAuth trial."""

    auth_json_path: str
    auth_return_path: str
    agent_import_path: str = AGENT_IMPORT_PATH
    n_concurrent_trials: int = 1
    max_retries: int = 0

    def agent_extra_env(self) -> dict[str, str]:
        """Return path-only agent configuration; credential bytes are never included."""
        return {
            CODEX_AUTH_JSON_PATH: self.auth_json_path,
            CODEX_AUTH_RETURN_PATH: self.auth_return_path,
        }

    def agent_kwargs(self) -> dict[str, dict[str, str]]:
        """Return kwargs accepted by Harbor's installed Codex agent."""
        return {"extra_env": self.agent_extra_env()}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_file(path: str, *, label: str) -> bytes:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise HarborOAuthSetupError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HarborOAuthSetupError(f"{label} must be a regular file")
    with open(path, "rb") as fh:
        return fh.read()


def _atomic_replace_bytes(path: str, content: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".harbor-auth-persist-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def persist_auth_file_cas(
    copy_path: str,
    master_path: str,
    expected_sha256: str,
) -> bool:
    """Persist a valid rotation only if the staged master generation is current."""
    updated = _read_regular_file(copy_path, label="returned auth.json")
    resolved_master = os.path.realpath(master_path)

    with auth_persist._lock(resolved_master):
        try:
            with open(resolved_master, "rb") as fh:
                current = fh.read()
        except FileNotFoundError as exc:
            raise HarborOAuthSetupError("auth master disappeared during the trial") from exc

        if _sha256(current) != expected_sha256:
            raise StaleCredentialError(
                "auth master changed after Harbor staged its credential; "
                "refusing stale persist-back"
            )
        if current == updated:
            return False

        # Reuse OpenBench's schema and immutable-account validator.
        auth_persist._validated_auth_bytes(current, updated)
        _atomic_replace_bytes(resolved_master, updated)
    return True


class HarborOAuthCredential:
    """Stage, configure, persist, and clean one Harbor Codex OAuth trial."""

    def __init__(self, master_path: str | os.PathLike[str]):
        self.master_path = os.path.abspath(os.fspath(master_path))
        self._resolved_master = os.path.realpath(self.master_path)
        self._temp_dir: str | None = None
        self._input_path: str | None = None
        self._return_path: str | None = None
        self._expected_sha256: str | None = None
        self._run_lock_fd: int | None = None
        self._entered = False
        self.persisted = False

    def _acquire_run_lock(self) -> None:
        if fcntl is None:
            raise HarborOAuthSetupError(
                "Harbor OAuth credential locking requires fcntl on a Unix host"
            )
        lock_path = self._resolved_master + ".harbor-oauth-run.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ConcurrentCredentialUseError(
                "a Harbor OAuth trial already owns this credential"
            ) from exc
        self._run_lock_fd = fd

    def _release_run_lock(self) -> None:
        if self._run_lock_fd is None:
            return
        try:
            fcntl.flock(self._run_lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._run_lock_fd)
            self._run_lock_fd = None

    def __enter__(self) -> "HarborOAuthCredential":
        if self._entered:
            raise HarborOAuthSetupError("HarborOAuthCredential contexts are single-use")
        self._entered = True
        try:
            self._acquire_run_lock()
            original = _read_regular_file(
                self._resolved_master, label="Codex auth master"
            )
            self._expected_sha256 = _sha256(original)
            self._temp_dir = tempfile.mkdtemp(prefix="obench_harbor_oauth_")
            os.chmod(self._temp_dir, 0o700)
            self._input_path = os.path.join(self._temp_dir, "auth.json")
            self._return_path = os.path.join(self._temp_dir, "auth-return.json")
            with open(self._input_path, "wb") as fh:
                fh.write(original)
            os.chmod(self._input_path, 0o600)
            return self
        except BaseException:
            self._cleanup()
            raise

    @property
    def config(self) -> HarborOAuthConfig:
        if not self._entered or not self._input_path or not self._return_path:
            raise HarborOAuthSetupError("enter the OAuth credential context first")
        return HarborOAuthConfig(
            auth_json_path=self._input_path,
            auth_return_path=self._return_path,
        )

    def _persist_returned_auth(self) -> None:
        if not self._return_path or not os.path.isfile(self._return_path):
            raise MissingAuthReturnError(
                "Harbor Codex did not return auth.json before cleanup"
            )
        os.chmod(self._return_path, 0o600)
        self.persisted = persist_auth_file_cas(
            self._return_path,
            self._resolved_master,
            self._expected_sha256 or "",
        )

    def _cleanup(self) -> None:
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None
        self._input_path = None
        self._return_path = None
        self._expected_sha256 = None
        self._release_run_lock()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if self._return_path and os.path.isfile(self._return_path):
                self._persist_returned_auth()
            elif not isinstance(exc, HarborOAuthError):
                raise MissingAuthReturnError(
                    "Harbor Codex did not return auth.json before cleanup"
                ) from exc
        finally:
            self._cleanup()
        return False


def build_harbor_oauth_context(
    master_path: str | os.PathLike[str],
) -> HarborOAuthCredential:
    """Build the single-trial OAuth context without importing Harbor."""
    return HarborOAuthCredential(master_path)
