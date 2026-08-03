"""Lazy Harbor Codex subclass with fail-closed OAuth persist-back capture."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from obench.harbor_oauth import (
    CODEX_AUTH_JSON_PATH,
    CODEX_AUTH_RETURN_PATH,
    HarborOAuthCaptureError,
    HarborOAuthSetupError,
    HarborOAuthUnsupportedError,
)
from obench.harbor_metering import (
    HARBOR_BASE_URL_ENV,
    HarborMeteringSession,
    UsageCounters,
)


def _load_harbor_codex():
    try:
        from harbor.agents.installed.codex import Codex
    except (ImportError, ModuleNotFoundError) as exc:
        raise HarborOAuthSetupError(
            "Harbor OAuth support requires the optional 'harbor' package; "
            "install a compatible Harbor release before loading "
            "obench.harbor_agents.codex:OpenBenchCodexOAuth"
        ) from exc
    return Codex


def _validate_host_return_path(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    try:
        parent_info = os.stat(parent)
    except OSError as exc:
        raise HarborOAuthSetupError(
            "OAuth auth-return parent directory is unavailable"
        ) from exc
    if stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise HarborOAuthSetupError(
            "OAuth auth-return parent directory must have mode 0700"
        )


def _refresh_staged_auth(return_path: str, input_path: str) -> None:
    """Atomically advance the staged input while retaining the return copy."""
    return_file = Path(return_path)
    input_file = Path(input_path)
    if return_file.parent.resolve() != input_file.parent.resolve():
        raise HarborOAuthSetupError(
            "OAuth input and return files must share one private staging directory"
        )
    try:
        source_fd = os.open(
            return_file,
            os.O_RDONLY | (getattr(os, "O_NOFOLLOW", 0)),
        )
    except OSError as exc:
        raise HarborOAuthSetupError("returned OAuth auth.json is unavailable") from exc
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise HarborOAuthSetupError(
                "returned OAuth auth.json must be a regular file"
            )
        chunks = []
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
    finally:
        os.close(source_fd)

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=".auth-next-",
        dir=input_file.parent,
    )
    try:
        os.fchmod(temp_fd, 0o600)
        offset = 0
        while offset < len(content):
            offset += os.write(temp_fd, content[offset:])
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(temp_name, input_file)
        os.chmod(input_file, 0o600)
        directory_fd = os.open(input_file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _build_agent_class(harbor_codex):
    required = ("_REMOTE_CODEX_HOME", "_REMOTE_CODEX_SECRETS_DIR", "exec_as_agent")
    missing = [name for name in required if not hasattr(harbor_codex, name)]
    if missing:
        raise HarborOAuthUnsupportedError(
            "installed Harbor Codex lacks required OAuth lifecycle members: "
            + ", ".join(missing)
        )

    class OpenBenchCodexOAuth(harbor_codex):
        """Harbor Codex with an opt-in pre-cleanup auth.json return hook."""

        def __init__(self, *args, **kwargs):
            # Harbor calls exec_as_agent during setup, before run() owns the
            # OAuth return lifecycle.
            self._oauth_run_active = False
            self._oauth_return_path = None
            self._oauth_capture_attempted = False
            self._oauth_capture_error = None
            super().__init__(*args, **kwargs)

        def _is_oauth_cleanup(self, command: str) -> bool:
            secrets_dir = PurePosixPath(self._REMOTE_CODEX_SECRETS_DIR).as_posix()
            return (
                "rm -rf" in command
                and secrets_dir in command
                and '"$CODEX_HOME"' in command
            )

        async def _capture_rotated_auth(self, environment) -> None:
            target = self._oauth_return_path
            remote_auth = (
                PurePosixPath(self._REMOTE_CODEX_SECRETS_DIR) / "auth.json"
            ).as_posix()
            try:
                await environment.download_file(remote_auth, target)
                os.chmod(target, 0o600)
                info = os.lstat(target)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise OSError("downloaded auth return is not a regular file")
                staged_input = self._get_env(CODEX_AUTH_JSON_PATH)
                if not staged_input:
                    raise OSError("staged OAuth input path is unavailable")
                _refresh_staged_auth(target, staged_input)
            except BaseException as exc:
                self._oauth_capture_error = HarborOAuthCaptureError(
                    "failed to return Harbor Codex auth.json before cleanup"
                )
                raise self._oauth_capture_error from exc

        async def exec_as_agent(
            self,
            environment,
            command,
            env=None,
            cwd=None,
            timeout_sec=None,
        ):
            capture_error = None
            if self._oauth_run_active and self._is_oauth_cleanup(command):
                self._oauth_capture_attempted = True
                try:
                    await self._capture_rotated_auth(environment)
                except HarborOAuthCaptureError as exc:
                    capture_error = exc

            result = None
            cleanup_error = None
            try:
                result = await super().exec_as_agent(
                    environment,
                    command,
                    env=env,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                )
            except BaseException as exc:
                cleanup_error = exc

            if capture_error is not None:
                raise capture_error from cleanup_error
            if cleanup_error is not None:
                raise cleanup_error
            return result

        async def run(self, instruction, environment, context):
            if getattr(self, "_oauth_run_active", False):
                raise HarborOAuthSetupError(
                    "one OpenBenchCodexOAuth instance cannot run concurrently"
                )
            auth_path = self._get_env(CODEX_AUTH_JSON_PATH)
            return_path = self._get_env(CODEX_AUTH_RETURN_PATH)
            if not auth_path or Path(auth_path).name != "auth.json":
                raise HarborOAuthSetupError(
                    "CODEX_AUTH_JSON_PATH must point to staged auth.json"
                )
            if not return_path:
                raise HarborOAuthSetupError(
                    f"{CODEX_AUTH_RETURN_PATH} is required for OAuth persist-back"
                )
            _validate_host_return_path(return_path)

            logs_dir = Path(self.logs_dir)
            trial_id = str(
                getattr(context, "trial_name", None)
                or logs_dir.parent.name
                or "harbor-trial"
            )
            metering = HarborMeteringSession(
                logs_dir / "harbor-metering",
                trial_id,
            )
            missing = object()
            prior_base_url = self._extra_env.get(HARBOR_BASE_URL_ENV, missing)
            self._extra_env[HARBOR_BASE_URL_ENV] = metering.runtime_base_url
            self._oauth_run_active = True
            self._oauth_return_path = return_path
            self._oauth_capture_attempted = False
            self._oauth_capture_error = None
            try:
                try:
                    result = await super().run(instruction, environment, context)
                except BaseException as exc:
                    if self._oauth_capture_error is not None:
                        raise self._oauth_capture_error from exc
                    if not self._oauth_capture_attempted:
                        raise HarborOAuthCaptureError(
                            "Harbor Codex failed before returning auth.json"
                        ) from exc
                    raise
                if self._oauth_capture_error is not None:
                    raise self._oauth_capture_error
                if not self._oauth_capture_attempted:
                    raise HarborOAuthUnsupportedError(
                        "installed Harbor Codex did not execute the supported "
                        "pre-cleanup auth-return boundary"
                    )
                return result
            finally:
                try:
                    trajectory = None
                    try:
                        session_dir = self._get_session_dir()
                        if session_dir is not None:
                            trajectory = self._convert_events_to_trajectory(
                                session_dir
                            )
                    except Exception:
                        trajectory = None
                    metering.seal(
                        UsageCounters.from_atif_trajectory(trajectory),
                        proxy_required=True,
                    )
                finally:
                    metering.close()
                    if prior_base_url is missing:
                        self._extra_env.pop(HARBOR_BASE_URL_ENV, None)
                    else:
                        self._extra_env[HARBOR_BASE_URL_ENV] = prior_base_url
                    self._oauth_run_active = False
                    self._oauth_return_path = None

    OpenBenchCodexOAuth.__name__ = "OpenBenchCodexOAuth"
    OpenBenchCodexOAuth.__qualname__ = "OpenBenchCodexOAuth"
    OpenBenchCodexOAuth.__module__ = __name__
    return OpenBenchCodexOAuth


def load_agent_class():
    """Load Harbor lazily and return the compatible Codex OAuth subclass."""
    return _build_agent_class(_load_harbor_codex())


def __getattr__(name):
    if name != "OpenBenchCodexOAuth":
        raise AttributeError(name)
    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
