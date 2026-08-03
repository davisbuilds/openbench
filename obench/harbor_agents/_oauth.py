"""Shared file-boundary checks for Harbor installed-agent OAuth wrappers."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from obench.harbor_oauth import (
    HarborOAuthCaptureError,
    HarborOAuthSetupError,
)


def resolve_auth_paths(
    agent,
    *,
    input_env: str,
    return_env: str,
) -> tuple[Path, Path]:
    """Resolve private host paths without reading credential contents."""

    input_value = agent._get_env(input_env)
    return_value = agent._get_env(return_env)
    if not input_value:
        raise HarborOAuthSetupError(f"{input_env} is required")
    if not return_value:
        raise HarborOAuthSetupError(f"{return_env} is required")

    input_path = Path(input_value)
    return_path = Path(return_value)
    if not input_path.is_absolute() or not return_path.is_absolute():
        raise HarborOAuthSetupError("OAuth staging paths must be absolute")
    if input_path == return_path:
        raise HarborOAuthSetupError("OAuth input and return paths must be distinct")
    try:
        input_info = input_path.lstat()
    except OSError as exc:
        raise HarborOAuthSetupError("staged OAuth auth.json is unavailable") from exc
    if stat.S_ISLNK(input_info.st_mode) or not stat.S_ISREG(input_info.st_mode):
        raise HarborOAuthSetupError(
            "staged OAuth auth.json must be a regular file"
        )

    try:
        parent_info = return_path.parent.stat()
    except OSError as exc:
        raise HarborOAuthSetupError(
            "OAuth auth-return parent directory is unavailable"
        ) from exc
    if stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise HarborOAuthSetupError(
            "OAuth auth-return parent directory must have mode 0700"
        )
    if return_path.exists() or return_path.is_symlink():
        try:
            return_info = return_path.lstat()
        except OSError as exc:
            raise HarborOAuthSetupError(
                "OAuth auth-return path is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(return_info.st_mode)
            or not stat.S_ISREG(return_info.st_mode)
            or stat.S_IMODE(return_info.st_mode) != 0o600
        ):
            raise HarborOAuthSetupError(
                "existing OAuth auth-return must be a mode-0600 regular file"
            )
    return input_path, return_path


async def upload_auth_json(
    agent,
    environment,
    *,
    input_path: Path,
    remote_path: str,
) -> None:
    await environment.upload_file(input_path, remote_path)
    if environment.default_user is not None:
        await agent.exec_as_root(
            environment,
            command=f"chown {environment.default_user} {remote_path}",
        )
    await agent.exec_as_agent(
        environment,
        command=f"chmod 600 {remote_path}",
    )


async def capture_auth_json(
    environment,
    *,
    remote_path: str,
    return_path: Path,
    harness: str,
) -> None:
    """Download one rotated file and verify its host-side file type/mode."""

    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{return_path.name}.capture-",
            dir=return_path.parent,
        )
        os.close(fd)
        temp_path = Path(raw_temp_path)
        await environment.download_file(remote_path, temp_path)
        os.chmod(temp_path, 0o600)
        info = temp_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("downloaded auth return is not a regular file")
        os.replace(temp_path, return_path)
        temp_path = None
    except BaseException as exc:
        raise HarborOAuthCaptureError(
            f"failed to return Harbor {harness} auth.json before cleanup"
        ) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def refresh_staged_auth(input_path: Path, return_path: Path) -> None:
    """Atomically advance the shared staged input while retaining the return."""

    try:
        content = return_path.read_bytes()
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{input_path.name}.refresh-",
            dir=input_path.parent,
        )
        temp_path = Path(raw_temp_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, input_path)
            temp_path = None
            os.chmod(input_path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    except BaseException as exc:
        raise HarborOAuthCaptureError(
            "failed to refresh staged OAuth auth.json after capture"
        ) from exc
