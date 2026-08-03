"""Host-side lifecycle for an optional Harbor Codex OAuth credential.

This module is stdlib-only. It does not import Harbor; the optional agent class
under :mod:`obench.harbor_agents.codex` performs that import lazily.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import stat
import tempfile

from . import auth_persist


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


def _read_regular_file(path: str, *, label: str) -> bytes:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise HarborOAuthSetupError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HarborOAuthSetupError(f"{label} must be a regular file")
    with open(path, "rb") as fh:
        return fh.read()


def persist_auth_file_cas(
    copy_path: str,
    master_path: str,
    expected_sha256: str,
) -> bool:
    """Persist a valid rotation only if the staged master generation is current."""
    _read_regular_file(copy_path, label="returned auth.json")
    try:
        with auth_persist.auth_file_lease(master_path) as lease:
            if lease.generation != expected_sha256:
                raise StaleCredentialError(
                    "auth master changed after Harbor staged its credential; "
                    "refusing stale persist-back"
                )
            return lease.persist(copy_path)
    except FileNotFoundError as exc:
        raise HarborOAuthSetupError(
            "auth master disappeared during the trial"
        ) from exc
    except auth_persist.StaleCredentialGenerationError as exc:
        raise StaleCredentialError(
            "auth master changed after Harbor staged its credential; "
            "refusing stale persist-back"
        ) from exc


class HarborOAuthCredential:
    """Stage, configure, persist, and clean one Harbor Codex OAuth trial."""

    def __init__(self, master_path: str | os.PathLike[str]):
        self.master_path = os.path.abspath(os.fspath(master_path))
        self._resolved_master = os.path.realpath(self.master_path)
        self._temp_dir: str | None = None
        self._input_path: str | None = None
        self._return_path: str | None = None
        self._lease: auth_persist.AuthFileLease | None = None
        self._entered = False
        self.persisted = False

    def _acquire_lease(self) -> None:
        lease = auth_persist.auth_file_lease(
            self._resolved_master, blocking=False
        )
        try:
            lease.__enter__()
        except auth_persist.CredentialLeaseUnavailableError as exc:
            raise ConcurrentCredentialUseError(
                "another OpenBench or Harbor run already owns this credential"
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise HarborOAuthSetupError(
                "Harbor OAuth credential locking is unavailable"
            ) from exc
        self._lease = lease

    def _release_lease(self) -> None:
        if self._lease is None:
            return
        lease = self._lease
        self._lease = None
        lease.__exit__(None, None, None)

    def __enter__(self) -> "HarborOAuthCredential":
        if self._entered:
            raise HarborOAuthSetupError("HarborOAuthCredential contexts are single-use")
        self._entered = True
        try:
            self._acquire_lease()
            self._temp_dir = tempfile.mkdtemp(prefix="obench_harbor_oauth_")
            os.chmod(self._temp_dir, 0o700)
            self._input_path = os.path.join(self._temp_dir, "auth.json")
            self._return_path = os.path.join(self._temp_dir, "auth-return.json")
            self._lease.stage(self._input_path)
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
        try:
            self.persisted = self._lease.persist(self._return_path)
        except auth_persist.StaleCredentialGenerationError as exc:
            raise StaleCredentialError(
                "auth master changed after Harbor staged its credential; "
                "refusing stale persist-back"
            ) from exc

    def _cleanup(self) -> None:
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None
        self._input_path = None
        self._return_path = None
        self._release_lease()

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
