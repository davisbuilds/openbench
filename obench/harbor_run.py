"""Run one Harbor trial with OpenBench-managed Codex OAuth credentials."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from contextlib import ExitStack
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
from obench.harbor_job import (
    AgentProfile,
    ConcurrencyPolicy,
    HARBOR_GIT_COMMIT,
    HarborJobArtifact,
    HarborJobSpec,
    LocalTaskSet,
    RetryPolicy,
    build_command_plan,
    build_job_config,
    write_job_config,
)
from obench.harbor_profiles import (
    AUTH_STRATEGY_OAUTH,
    resolve_harbor_profile,
)
from obench.harbor_agents._subscription import staged_subscription_auth

HARBOR_VERSION = "0.20.0"
HARBOR_TASK_SCHEMA_VERSION = "1.4"
OPENBENCH_TASK_ORIGIN = "openbench"
OPENBENCH_WORKSPACE_ARTIFACT = {
    "source": "/app",
    "destination": "workspace",
}
HARBOR_DEFAULT_NETWORK_MODE = "public"
_JOB_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

ProcessRunner = Callable[..., Any]
Which = Callable[[str], str | None]


class HarborRunError(RuntimeError):
    """Raised when a Harbor invocation cannot be prepared safely."""


@dataclass(frozen=True)
class HarborBinary:
    path: Path
    version: str
    git_commit: str
    is_editable: bool


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


@dataclass(frozen=True)
class HarborProfileJobResult:
    returncode: int
    artifact: HarborJobArtifact
    config_path: Path
    expected_job_path: Path
    resumes_existing_job: bool


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
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarborRunError(f"Cannot read Harbor task.toml: {task_toml}") from exc
    if config.get("schema_version") != HARBOR_TASK_SCHEMA_VERSION:
        raise HarborRunError(
            f"Harbor task.toml must use schema_version "
            f"{HARBOR_TASK_SCHEMA_VERSION}: {task_toml}"
        )
    metadata = config.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("origin") != OPENBENCH_TASK_ORIGIN:
        raise HarborRunError(
            f"Harbor task.toml must define [metadata].origin = "
            f"{OPENBENCH_TASK_ORIGIN!r}: {task_toml}"
        )
    if config.get("artifacts") != [OPENBENCH_WORKSPACE_ARTIFACT]:
        raise HarborRunError(
            "Harbor task.toml must configure exactly one /app to workspace "
            f"artifact: {task_toml}"
        )
    task = config.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise HarborRunError(f"Harbor task.toml must define [task].name: {task_toml}")
    if not task["name"].strip():
        raise HarborRunError(f"Harbor [task].name must not be empty: {task_toml}")
    environment = config.get("environment")
    agent = config.get("agent")
    environment_network_mode = (
        environment.get("network_mode")
        if isinstance(environment, dict)
        else None
    )
    agent_network_mode = (
        agent.get("network_mode")
        if isinstance(agent, dict)
        else None
    )
    effective_network_mode = (
        agent_network_mode
        if agent_network_mode is not None
        else environment_network_mode
    )
    if effective_network_mode is None:
        effective_network_mode = HARBOR_DEFAULT_NETWORK_MODE
    if not isinstance(effective_network_mode, str):
        raise HarborRunError(
            f"Harbor effective agent network_mode must be a string: {task_toml}"
        )
    if effective_network_mode == "no-network":
        raise HarborRunError(
            "Codex OAuth requires public agent networking; export the task "
            f"with --network-mode public: {task_toml}"
        )
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

    try:
        with resolved.open("rb") as handle:
            first_line = handle.readline(4096).decode(
                "utf-8", "strict"
            ).strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise HarborRunError(
            f"Cannot inspect Harbor executable shebang: {resolved}"
        ) from exc
    if not first_line.startswith("#!"):
        raise HarborRunError(
            "Harbor executable must expose its package interpreter in a shebang"
        )
    interpreter = Path(first_line[2:].strip())
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise HarborRunError(
            "Harbor executable must use an absolute package interpreter"
        )

    metadata_script = (
        "import json;"
        "from harbor.models.job.lock import "
        "_get_harbor_git_commit_hash,_get_harbor_is_editable_install,"
        "_get_harbor_version;"
        "print(json.dumps({'version':_get_harbor_version(),"
        "'git_commit':_get_harbor_git_commit_hash(),"
        "'is_editable':_get_harbor_is_editable_install()},sort_keys=True))"
    )
    try:
        metadata_process = run_process(
            [str(interpreter), "-c", metadata_script],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise HarborRunError(
            "Cannot inspect installed Harbor package metadata"
        ) from exc
    if metadata_process.returncode != 0:
        raise HarborRunError(
            "Harbor package metadata preflight failed with exit code "
            f"{metadata_process.returncode}"
        )
    try:
        metadata = json.loads(metadata_process.stdout or "")
    except json.JSONDecodeError as exc:
        raise HarborRunError(
            "Harbor package metadata preflight returned invalid JSON"
        ) from exc
    expected_metadata = {
        "version": HARBOR_VERSION,
        "git_commit": HARBOR_GIT_COMMIT,
        "is_editable": False,
    }
    if metadata != expected_metadata:
        raise HarborRunError(
            "Harbor package provenance mismatch; required "
            f"version={HARBOR_VERSION}, commit={HARBOR_GIT_COMMIT}, "
            "editable=false"
        )
    return HarborBinary(
        path=resolved,
        version=HARBOR_VERSION,
        git_commit=HARBOR_GIT_COMMIT,
        is_editable=False,
    )


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
    requested_jobs_path = Path(jobs_dir).expanduser()
    if requested_jobs_path.is_symlink():
        raise HarborRunError(
            f"Jobs path must not be a symlink: {requested_jobs_path}"
        )
    jobs_path = requested_jobs_path.resolve()
    if jobs_path.exists() and not jobs_path.is_dir():
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


def run_harbor_profile_job(
    *,
    exported_tasks_dir: str | os.PathLike[str],
    task_names: tuple[str, ...],
    harnesses: tuple[str, ...],
    model: str,
    attempts: int,
    n_concurrent_trials: int,
    max_retries: int,
    jobs_dir: str | os.PathLike[str],
    job_name: str,
    config_path: str | os.PathLike[str],
    harbor_binary: str | os.PathLike[str] = "harbor",
    run_process: ProcessRunner = subprocess.run,
    which: Which = shutil.which,
) -> HarborProfileJobResult:
    """Run one native Harbor task x harness x attempt matrix with OAuth leases."""

    if not harnesses or len(set(harnesses)) != len(harnesses):
        raise HarborRunError("Harnesses must be a nonempty unique sequence")
    if not task_names or len(set(task_names)) != len(task_names):
        raise HarborRunError("Task names must be a nonempty unique sequence")
    harbor = preflight_harbor_binary(
        harbor_binary, run_process=run_process, which=which
    )
    process_env = dict(os.environ)
    job_profiles: list[AgentProfile] = []
    oauth_configs: list[HarborOAuthConfig] = []

    with ExitStack() as stack:
        for harness in harnesses:
            profile = resolve_harbor_profile(harness, model)
            env_prefix = f"OPENBENCH_HARBOR_{harness.upper()}_AUTH"
            input_source_env = f"{env_prefix}_INPUT"
            profile_env: dict[str, str]
            if profile.auth.strategy == AUTH_STRATEGY_OAUTH:
                master_path = _resolve_profile_auth_source(
                    harness, profile.auth.source_candidates
                )
                credential = stack.enter_context(
                    HarborOAuthCredential(master_path)
                )
                oauth = credential.config
                oauth_configs.append(oauth)
                return_source_env = f"{env_prefix}_RETURN"
                process_env[input_source_env] = oauth.auth_json_path
                process_env[return_source_env] = oauth.auth_return_path
                if profile.auth.return_env is None:
                    raise HarborRunError(
                        f"{harness} OAuth profile lacks a return environment"
                    )
                profile_env = {
                    profile.auth.input_env: f"${{{input_source_env}}}",
                    profile.auth.return_env: f"${{{return_source_env}}}",
                }
            else:
                archive = stack.enter_context(
                    staged_subscription_auth(
                        harness,
                        profile.auth.source_candidates,
                    )
                )
                process_env[input_source_env] = str(archive)
                profile_env = {
                    profile.auth.input_env: f"${{{input_source_env}}}",
                }
            job_profiles.append(
                AgentProfile(
                    profile_id=harness,
                    model_name=profile.harbor_model_name,
                    import_path=profile.agent_import_path,
                    n_concurrent=profile.auth.max_concurrent_uses,
                    concurrency_group=profile.auth.concurrency_group,
                    kwargs=profile.agent_kwargs(),
                    env=profile_env,
                )
            )

        artifact = build_job_config(
            HarborJobSpec(
                job_name=job_name,
                jobs_dir=jobs_dir,
                source=LocalTaskSet(
                    exported_tasks_dir,
                    task_names=task_names,
                ),
                agent_profiles=tuple(job_profiles),
                models=(),
                attempts=attempts,
                concurrency=ConcurrencyPolicy(
                    n_concurrent_trials=n_concurrent_trials
                ),
                retry=RetryPolicy(max_retries=max_retries),
            )
        )
        written_config = write_job_config(artifact, config_path)
        plan = build_command_plan(
            artifact,
            written_config,
            harbor_binary=harbor.path,
        )
        if plan.resumes_existing_job:
            for oauth in oauth_configs:
                _seed_resume_auth_return(oauth)
        completed = run_process(
            list(plan.argv),
            check=False,
            env=process_env,
        )

    return HarborProfileJobResult(
        returncode=int(completed.returncode),
        artifact=artifact,
        config_path=written_config,
        expected_job_path=plan.expected_job_path,
        resumes_existing_job=plan.resumes_existing_job,
    )


def _resolve_profile_auth_source(
    harness: str, candidates: tuple[str, ...]
) -> Path:
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and not path.is_symlink():
            return path.resolve()
    rendered = ", ".join(candidates)
    raise HarborRunError(
        f"{harness} OAuth credential is unavailable; checked: {rendered}"
    )


def _seed_resume_auth_return(oauth: HarborOAuthConfig) -> None:
    """Allow a no-op resume to persist the unchanged staged generation."""

    source = Path(oauth.auth_json_path)
    destination = Path(oauth.auth_return_path)
    if destination.exists() or destination.is_symlink():
        raise HarborRunError(
            f"OAuth resume return path already exists: {destination}"
        )
    try:
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    except OSError as exc:
        raise HarborRunError(
            "Cannot seed OAuth return path for Harbor resume"
        ) from exc


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
