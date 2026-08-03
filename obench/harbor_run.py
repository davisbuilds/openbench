"""Run one Harbor trial with OpenBench-managed Codex OAuth credentials."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obench.harbor_oauth import (
    AGENT_IMPORT_PATH,
    CODEX_AUTH_JSON_PATH,
    CODEX_AUTH_RETURN_PATH,
    HarborOAuthConfig,
    HarborOAuthCredential,
)

HARBOR_VERSION = "0.20.0"
_JOB_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

ProcessRunner = Callable[..., Any]
Which = Callable[[str], str | None]


class HarborRunError(RuntimeError):
    """Raised when a Harbor invocation cannot be prepared safely."""


@dataclass(frozen=True)
class HarborBinary:
    path: Path
    version: str


@dataclass(frozen=True)
class HarborRunPlan:
    argv: tuple[str, ...]
    task_path: Path
    expected_job_path: Path
    harbor_version: str


@dataclass(frozen=True)
class HarborRunResult:
    returncode: int
    plan: HarborRunPlan

    @property
    def expected_job_path(self) -> Path:
        return self.plan.expected_job_path


def validate_task_root(task_dir: str | os.PathLike[str]) -> Path:
    """Return an absolute Harbor task root, rejecting dataset-style roots."""

    root = Path(task_dir).expanduser()
    if not root.is_dir():
        raise HarborRunError(f"Harbor task root is not a directory: {root}")
    if root.is_symlink():
        raise HarborRunError(f"Harbor task root must not be a symlink: {root}")
    root = root.resolve()

    task_toml = root / "task.toml"
    if not task_toml.is_file() or task_toml.is_symlink():
        raise HarborRunError(
            f"Harbor task root must contain a regular task.toml: {root}"
        )
    instruction = root / "instruction.md"
    if not instruction.is_file() or instruction.is_symlink():
        raise HarborRunError(
            f"Harbor task root must contain a regular instruction.md: {root}"
        )

    nested_task_files: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories if not (current_path / name).is_symlink()
        ]
        candidate = current_path / "task.toml"
        if "task.toml" in files and candidate != task_toml:
            nested_task_files.append(candidate)
    if nested_task_files:
        raise HarborRunError(
            "Expected one Harbor task root, but found nested task.toml: "
            + ", ".join(str(path) for path in sorted(nested_task_files))
        )

    try:
        with task_toml.open("rb") as handle:
            metadata = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarborRunError(f"Cannot read Harbor task.toml: {task_toml}") from exc
    task = metadata.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise HarborRunError(f"Harbor task.toml must define [task].name: {task_toml}")
    if not task["name"].strip():
        raise HarborRunError(f"Harbor [task].name must not be empty: {task_toml}")
    return root


def preflight_harbor_binary(
    harbor_binary: str | os.PathLike[str] = "harbor",
    *,
    run_process: ProcessRunner = subprocess.run,
    which: Which = shutil.which,
) -> HarborBinary:
    """Resolve Harbor and require the exact CLI contract used by this runner."""

    requested = os.fspath(harbor_binary)
    if not requested:
        raise HarborRunError("Harbor binary must be specified")

    if os.sep in requested or (os.altsep and os.altsep in requested):
        resolved = Path(requested).expanduser()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise HarborRunError(f"Harbor binary is not executable: {resolved}")
        resolved = resolved.resolve()
    else:
        found = which(requested)
        if found is None:
            raise HarborRunError(f"Harbor binary not found on PATH: {requested}")
        resolved = Path(found).resolve()

    try:
        completed = run_process(
            [str(resolved), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise HarborRunError(f"Cannot execute Harbor binary: {resolved}") from exc
    if completed.returncode != 0:
        raise HarborRunError(
            f"Harbor version preflight failed with exit code {completed.returncode}"
        )

    output = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
    if output != HARBOR_VERSION:
        reported = output or "<no version output>"
        raise HarborRunError(
            f"Harbor {HARBOR_VERSION} is required; binary reported: {reported}"
        )
    return HarborBinary(path=resolved, version=HARBOR_VERSION)


def build_harbor_oauth_command(
    *,
    harbor: HarborBinary,
    task_path: Path,
    model: str,
    jobs_dir: Path,
    job_name: str,
    oauth: HarborOAuthConfig,
) -> HarborRunPlan:
    """Build the fixed one-trial Harbor argv from a staged OAuth context."""

    model = _validate_model(model)
    job_name = _validate_job_name(job_name)
    jobs_dir = jobs_dir.expanduser().resolve()
    expected_job_path = jobs_dir / job_name

    agent_env = oauth.agent_extra_env()
    expected_keys = {CODEX_AUTH_JSON_PATH, CODEX_AUTH_RETURN_PATH}
    if set(agent_env) != expected_keys:
        raise HarborRunError("OAuth context returned an unsupported agent environment")
    for key, value in agent_env.items():
        if not Path(value).is_absolute():
            raise HarborRunError(f"OAuth agent environment must be path-valued: {key}")

    argv = (
        str(harbor.path),
        "run",
        "-p",
        str(task_path),
        "-a",
        AGENT_IMPORT_PATH,
        "-m",
        model,
        "-k",
        "1",
        "-n",
        "1",
        "-r",
        "0",
        "-o",
        str(jobs_dir),
        "--job-name",
        job_name,
        "--ae",
        f"{CODEX_AUTH_JSON_PATH}={agent_env[CODEX_AUTH_JSON_PATH]}",
        "--ae",
        f"{CODEX_AUTH_RETURN_PATH}={agent_env[CODEX_AUTH_RETURN_PATH]}",
    )
    return HarborRunPlan(
        argv=argv,
        task_path=task_path,
        expected_job_path=expected_job_path,
        harbor_version=harbor.version,
    )


def run_harbor_oauth(
    *,
    task_dir: str | os.PathLike[str],
    model: str,
    master_auth_json: str | os.PathLike[str],
    jobs_dir: str | os.PathLike[str],
    job_name: str,
    harbor_binary: str | os.PathLike[str] = "harbor",
    run_process: ProcessRunner = subprocess.run,
    which: Which = shutil.which,
) -> HarborRunResult:
    """Run one trial and persist its returned OAuth rotation on every exit path."""

    task_path = validate_task_root(task_dir)
    model = _validate_model(model)
    job_name = _validate_job_name(job_name)
    jobs_path = Path(jobs_dir).expanduser().resolve()
    if jobs_path.exists() and (not jobs_path.is_dir() or jobs_path.is_symlink()):
        raise HarborRunError(f"Jobs path must be a real directory: {jobs_path}")
    expected_job_path = jobs_path / job_name
    if expected_job_path.exists():
        raise HarborRunError(f"Expected Harbor job path already exists: {expected_job_path}")

    master_path = Path(master_auth_json).expanduser()
    if not master_path.is_file() or master_path.is_symlink():
        raise HarborRunError(f"Master auth.json must be a regular file: {master_path}")
    master_path = master_path.resolve()

    # Version compatibility is established before auth.json is copied to staging.
    harbor = preflight_harbor_binary(
        harbor_binary, run_process=run_process, which=which
    )
    jobs_path.mkdir(parents=True, exist_ok=True)

    with HarborOAuthCredential(master_path) as oauth:
        plan = build_harbor_oauth_command(
            harbor=harbor,
            task_path=task_path,
            model=model,
            jobs_dir=jobs_path,
            job_name=job_name,
            oauth=oauth.config,
        )
        completed = run_process(list(plan.argv), check=False)

    return HarborRunResult(returncode=int(completed.returncode), plan=plan)


def _validate_model(model: str) -> str:
    if not isinstance(model, str) or not model or model != model.strip():
        raise HarborRunError("An explicit model without surrounding whitespace is required")
    if "\0" in model:
        raise HarborRunError("Model must not contain NUL bytes")
    return model


def _validate_job_name(job_name: str) -> str:
    if not isinstance(job_name, str) or not _JOB_NAME_RE.fullmatch(job_name):
        raise HarborRunError(
            "Job name must start with an alphanumeric character and contain only "
            "letters, numbers, dot, underscore, or hyphen"
        )
    return job_name
