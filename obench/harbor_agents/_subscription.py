"""Private read-only subscription-auth staging for Harbor custom agents."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
from typing import Iterator

from obench.harbor_oauth import HarborOAuthSetupError


CURSOR_AUTH_ARCHIVE_ENV = "OPENBENCH_CURSOR_AUTH_ARCHIVE"
DEVIN_AUTH_ARCHIVE_ENV = "OPENBENCH_DEVIN_AUTH_ARCHIVE"


@contextmanager
def staged_subscription_auth(
    harness: str,
    candidates: tuple[str, ...],
) -> Iterator[Path]:
    """Build one private archive containing only a harness's login state."""

    with tempfile.TemporaryDirectory(
        prefix=f"openbench-harbor-{harness}-auth-"
    ) as raw_temp:
        temp = Path(raw_temp)
        temp.chmod(0o700)
        payload = temp / "payload"
        payload.mkdir(mode=0o700)
        if harness == "cursor":
            _stage_cursor(payload, candidates)
        elif harness == "devin":
            _stage_devin(payload, candidates)
        else:
            raise HarborOAuthSetupError(
                f"unsupported read-only subscription auth harness: {harness}"
            )

        archive = temp / f"{harness}-auth.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            for path in sorted(payload.rglob("*")):
                handle.add(path, arcname=path.relative_to(payload), recursive=False)
        archive.chmod(0o600)
        yield archive


def _stage_cursor(payload: Path, candidates: tuple[str, ...]) -> None:
    for raw_candidate in candidates:
        source = Path(raw_candidate).expanduser()
        if not _regular_file(source):
            continue
        if source.name == "auth.json":
            destination = payload / ".config" / "cursor" / "auth.json"
            _copy_private_file(source, destination)
            return
        if source.name == "cli-config.json":
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                auth_info = data["authInfo"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise HarborOAuthSetupError(
                    "Cursor cli-config.json does not contain valid authInfo"
                ) from exc
            destination = payload / ".cursor" / "cli-config.json"
            destination.parent.mkdir(parents=True, mode=0o700)
            destination.write_text(
                json.dumps({"authInfo": auth_info}, sort_keys=True),
                encoding="utf-8",
            )
            destination.chmod(0o600)
            return
    raise HarborOAuthSetupError(
        "Cursor subscription credential is unavailable; checked: "
        + ", ".join(candidates)
    )


def _stage_devin(payload: Path, candidates: tuple[str, ...]) -> None:
    found = False
    home = Path.home()
    for raw_candidate in candidates:
        source = Path(raw_candidate).expanduser()
        if not source.is_dir() or source.is_symlink():
            continue
        try:
            relative = source.resolve().relative_to(home.resolve())
        except ValueError as exc:
            raise HarborOAuthSetupError(
                f"Devin auth source must be inside the current home: {source}"
            ) from exc
        _copy_private_tree(source, payload / relative)
        found = True
    if not found:
        raise HarborOAuthSetupError(
            "Devin subscription credential is unavailable; checked: "
            + ", ".join(candidates)
        )


def _copy_private_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700)
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise HarborOAuthSetupError(
                    f"subscription auth contains a symlink: {path}"
                )
            (destination / path.relative_to(source)).mkdir(
                parents=True, exist_ok=True, mode=0o700
            )
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise HarborOAuthSetupError(
                    f"subscription auth contains a symlink: {path}"
                )
            if not _regular_file(path):
                raise HarborOAuthSetupError(
                    f"subscription auth contains a non-regular file: {path}"
                )
            _copy_private_file(
                path,
                destination / path.relative_to(source),
            )


def _copy_private_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def _regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def resolve_subscription_archive(agent, env_name: str) -> Path:
    """Resolve a private host archive path from a Harbor agent environment."""

    value = agent._get_env(env_name)
    if not value:
        raise HarborOAuthSetupError(f"{env_name} is required")
    path = Path(value)
    if not path.is_absolute() or not _regular_file(path):
        raise HarborOAuthSetupError(
            "subscription auth archive must be an absolute regular file"
        )
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise HarborOAuthSetupError(
            "subscription auth archive must have mode 0600"
        )
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise HarborOAuthSetupError(
            "subscription auth archive parent must have mode 0700"
        )
    return path


async def upload_subscription_archive(
    agent,
    environment,
    *,
    archive: Path,
    remote_archive: str,
) -> None:
    await environment.upload_file(archive, remote_archive)
    if environment.default_user is not None:
        await agent.exec_as_root(
            environment,
            command=(
                f"chown {environment.default_user} {remote_archive} && "
                f"chmod 600 {remote_archive}"
            ),
        )
