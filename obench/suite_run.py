"""Compile and execute Harbor-first OpenBench suites.

The semantic manifest is path-independent and credential-free. Runtime Harbor
job configs and credential paths remain local artifacts outside that manifest.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
from importlib import metadata
from importlib.machinery import all_suffixes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Callable

from .config import OpenBenchConfig, require_suite_config
from .harbor_agents._subscription import staged_subscription_auth
from .harbor_job import (
    ConcurrencyPolicy,
    Dataset,
    HARBOR_GIT_COMMIT,
    HARBOR_VERSION,
    HarborJobArtifact,
    HarborJobSpec,
    LocalTaskSet,
    RetryPolicy,
    build_command_plan,
    build_job_config,
    write_job_config,
)
from .harbor_oauth import HarborOAuthCredential, HarborOAuthError
from .harbor_profiles import (
    AUTH_STRATEGY_OAUTH,
    HarborHarnessProfile,
    resolve_harbor_profile,
)
from .harbor_run import (
    HarborBinary,
    HarborRunError,
    preflight_harbor_binary,
)
from .profile_spec import (
    CustomProfileSpec,
    ProfileRegistry,
    ProfileSpec,
    ProfileSpecError,
    StockProfileSpec,
    compile_profile,
    load_profile_registry,
)
from .suite import Arm, Suite, SuiteError, TaskSet, load_suite


MANIFEST_SCHEMA_VERSION = 1
_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_SEMVER_RE = re.compile(
    r"v?[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)

ProcessRunner = Callable[..., Any]
PostRunHook = Callable[[tuple["SuiteRunArtifact", ...]], None]


class SuiteRunError(RuntimeError):
    """A suite cannot be safely compiled or executed."""


class _NonzeroHarborExit(HarborOAuthError):
    """Unwind OAuth contexts without requiring a return after process failure."""


@dataclass(frozen=True)
class CompiledArm:
    arm: Arm
    profile: ProfileSpec
    agent: Any


@dataclass(frozen=True)
class CompiledTaskSet:
    task_set: TaskSet
    task_names: tuple[str, ...] | None
    logical_names: tuple[str, ...] | None
    content_sha256: str | None


@dataclass(frozen=True)
class CompiledSuite:
    suite: Suite
    config: OpenBenchConfig
    registry: ProfileRegistry
    arms: tuple[CompiledArm, ...]
    task_sets: tuple[CompiledTaskSet, ...]
    manifest: dict[str, Any]
    manifest_bytes: bytes
    manifest_sha256: str


@dataclass(frozen=True)
class PlannedJob:
    task_set_id: str
    artifact: HarborJobArtifact


@dataclass(frozen=True)
class SuiteRunArtifact:
    task_set_id: str
    returncode: int
    config_path: Path
    config_sha256: str
    harbor_job_path: Path
    resumed: bool


@dataclass(frozen=True)
class SuiteRunResult:
    manifest_path: Path
    manifest_sha256: str
    artifacts: tuple[SuiteRunArtifact, ...]

    @property
    def returncode(self) -> int:
        return next(
            (artifact.returncode for artifact in self.artifacts if artifact.returncode),
            0,
        )


def discover_suite(
    path: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
) -> tuple[Suite, OpenBenchConfig]:
    """Resolve an explicit suite or the nearest config's required default."""

    if path is None:
        try:
            config = require_suite_config(os.fspath(start) if start else None)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            raise SuiteRunError(str(exc)) from exc
        suite_path = config.default_suite
        assert suite_path is not None
        suite = load_suite(suite_path, project_root=config.project_root)
        return suite, config

    suite = load_suite(path)
    try:
        config = require_suite_config(os.fspath(suite.project_root))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise SuiteRunError(str(exc)) from exc
    if Path(config.project_root or "").resolve() != suite.project_root.resolve():
        raise SuiteRunError(
            "explicit suite and nearest .openbench/openbench.toml have "
            "different project roots"
        )
    return suite, config


def compile_suite(
    path: str | os.PathLike[str] | None = None,
    *,
    start: str | os.PathLike[str] | None = None,
) -> CompiledSuite:
    """Compile suite intent without staging auth or invoking Harbor."""

    suite, config = discover_suite(path, start=start)
    _validate_harbor_pin(suite)
    registry = load_profile_registry(suite.project_root)

    compiled_arms: list[CompiledArm] = []
    for arm in suite.arms:
        profile = registry.get(arm.profile)
        if isinstance(profile, StockProfileSpec) and arm.harness != profile.harness:
            raise SuiteRunError(
                f"arm {arm.id!r} harness {arm.harness!r} does not match "
                f"stock profile {profile.id!r} harness {profile.harness!r}"
            )
        agent = replace(compile_profile(profile, arm.model), profile_id=arm.id)
        compiled_arms.append(
            CompiledArm(arm=arm, profile=profile, agent=agent)
        )

    compiled_task_sets = tuple(
        _compile_task_set(task_set, suite.project_root)
        for task_set in suite.task_sets
    )
    _reject_task_collisions(compiled_task_sets)

    manifest = _semantic_manifest(
        suite,
        tuple(compiled_arms),
        compiled_task_sets,
    )
    _assert_semantic_manifest_safe(manifest)
    manifest_bytes = _canonical_json(manifest)
    return CompiledSuite(
        suite=suite,
        config=config,
        registry=registry,
        arms=tuple(compiled_arms),
        task_sets=compiled_task_sets,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def plan_jobs(compiled: CompiledSuite) -> tuple[PlannedJob, ...]:
    """Build one deterministic native Harbor job per declared task set."""

    jobs_dir = compiled.config.jobs_dir
    if jobs_dir is None:
        raise SuiteRunError("jobs_dir is required")
    profiles = tuple(item.agent for item in compiled.arms)
    jobs: list[PlannedJob] = []
    for item in compiled.task_sets:
        source: LocalTaskSet | Dataset
        if item.task_set.kind == "local":
            assert item.task_set.path is not None
            source = LocalTaskSet(
                item.task_set.path,
                task_names=item.task_names,
            )
        else:
            assert item.task_set.name is not None
            assert item.task_set.ref is not None
            if "/" in item.task_set.name:
                source = Dataset(
                    name=item.task_set.name,
                    ref=item.task_set.ref,
                )
            else:
                if _SEMVER_RE.fullmatch(item.task_set.ref) is None:
                    raise SuiteRunError(
                        f"registry task set {item.task_set.id!r} requires an "
                        "exact semantic version"
                    )
                source = Dataset(
                    name=item.task_set.name,
                    version=item.task_set.ref,
                )
        job_name = (
            f"{compiled.suite.id}-{item.task_set.id}-"
            f"{compiled.manifest_sha256[:12]}"
        )
        base_artifact = build_job_config(
            HarborJobSpec(
                job_name=job_name,
                jobs_dir=jobs_dir,
                source=source,
                agent_profiles=profiles,
                models=(),
                attempts=compiled.suite.run.attempts,
                concurrency=ConcurrencyPolicy(
                    n_concurrent_trials=compiled.suite.run.concurrency
                ),
                retry=RetryPolicy(
                    max_retries=compiled.suite.run.max_retries
                ),
            )
        )
        jobs.append(
            PlannedJob(
                task_set_id=item.task_set.id,
                artifact=_build_suite_job_artifact(
                    base_artifact,
                    compiled.arms,
                    compiled.suite.run.timeout_seconds,
                ),
            )
        )
    return tuple(jobs)


def run_suite(
    compiled: CompiledSuite,
    *,
    harbor_binary: str | os.PathLike[str] = "harbor",
    run_process: ProcessRunner = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    preflight: Callable[..., HarborBinary] = preflight_harbor_binary,
    post_run_hook: PostRunHook | None = None,
) -> SuiteRunResult:
    """Execute planned jobs while Harbor owns scheduling, retry, and resume."""

    jobs = plan_jobs(compiled)
    custom_env = _validate_custom_runtime(compiled)

    harbor = preflight(
        harbor_binary,
        run_process=run_process,
        which=which,
    )
    if (
        harbor.version != HARBOR_VERSION
        or harbor.git_commit != HARBOR_GIT_COMMIT
        or harbor.is_editable
    ):
        raise SuiteRunError("Harbor preflight did not return the pinned build")

    manifest_path = _write_manifest(compiled)
    process_env = dict(os.environ)
    process_env.update(custom_env)
    artifacts: list[SuiteRunArtifact] = []
    configs_dir = (
        Path(compiled.config.jobs_dir or "")
        / "suite-configs"
        / compiled.manifest_sha256
    )
    _ensure_safe_artifact_directory(
        compiled.suite.project_root,
        configs_dir,
        label="Harbor suite config directory",
    )

    try:
        with ExitStack() as stack:
            oauth_credentials = _stage_stock_credentials(
                stack, compiled, process_env
            )
            for job in jobs:
                config_path = configs_dir / f"{job.task_set_id}.json"
                written = write_job_config(job.artifact, config_path)
                command = build_command_plan(
                    job.artifact,
                    written,
                    harbor_binary=harbor.path,
                )
                no_op_resume = (
                    command.resumes_existing_job
                    and _completed_harbor_job(command.expected_job_path)
                )
                completed = run_process(
                    list(command.argv),
                    check=False,
                    env=process_env,
                )
                artifacts.append(
                    SuiteRunArtifact(
                        task_set_id=job.task_set_id,
                        returncode=int(completed.returncode),
                        config_path=written,
                        config_sha256=job.artifact.sha256,
                        harbor_job_path=command.expected_job_path,
                        resumed=command.resumes_existing_job,
                    )
                )
                if completed.returncode:
                    if _oauth_return_missing(oauth_credentials):
                        raise _NonzeroHarborExit(
                            f"Harbor exited with {completed.returncode} "
                            "before returning staged OAuth"
                        )
                    break
                if no_op_resume:
                    _seed_missing_oauth_returns(oauth_credentials)
    except _NonzeroHarborExit:
        pass

    result = SuiteRunResult(
        manifest_path=manifest_path,
        manifest_sha256=compiled.manifest_sha256,
        artifacts=tuple(artifacts),
    )
    if post_run_hook is not None:
        post_run_hook(result.artifacts)
    return result


def _validate_harbor_pin(suite: Suite) -> None:
    if (
        suite.harbor.version != HARBOR_VERSION
        or suite.harbor.commit != HARBOR_GIT_COMMIT
    ):
        raise SuiteRunError(
            "suite Harbor pin does not match OpenBench's pinned runtime: "
            f"required version={HARBOR_VERSION}, commit={HARBOR_GIT_COMMIT}"
        )


def _compile_task_set(task_set: TaskSet, root: Path) -> CompiledTaskSet:
    if task_set.kind != "local":
        return CompiledTaskSet(
            task_set=task_set,
            task_names=None,
            logical_names=None,
            content_sha256=None,
        )
    assert task_set.path is not None
    names: list[str] = []
    logical_names: list[str] = []
    for child in sorted(task_set.path.iterdir(), key=lambda value: value.name):
        if not child.is_dir():
            continue
        task_toml = child / "task.toml"
        instruction = child / "instruction.md"
        if not task_toml.is_file() or not instruction.is_file():
            continue
        names.append(child.name)
        try:
            with task_toml.open("rb") as handle:
                raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise SuiteRunError(f"{task_toml}: invalid TOML: {exc}") from exc
        task = raw.get("task")
        logical = task.get("name") if isinstance(task, dict) else None
        if (
            not isinstance(logical, str)
            or not logical
            or logical != logical.strip()
            or "\x00" in logical
        ):
            raise SuiteRunError(
                f"{task_toml}: task.name must be a non-empty safe string"
            )
        logical_names.append(logical)
    relative = task_set.path.relative_to(root).as_posix()
    return CompiledTaskSet(
        task_set=task_set,
        task_names=tuple(names),
        logical_names=tuple(logical_names),
        content_sha256=_tree_digest(task_set.path, relative),
    )


def _tree_digest(root: Path, semantic_root: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        mode = "x" if path.stat().st_mode & 0o111 else "-"
        content = path.read_bytes()
        for part in (
            semantic_root.encode("utf-8"),
            relative.encode("utf-8"),
            mode.encode("ascii"),
            hashlib.sha256(content).digest(),
        ):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def _reject_task_collisions(task_sets: tuple[CompiledTaskSet, ...]) -> None:
    seen: dict[str, str] = {}
    for item in task_sets:
        if item.logical_names is None:
            continue
        for logical_name in item.logical_names:
            previous = seen.get(logical_name)
            if previous is not None:
                raise SuiteRunError(
                    f"logical task {logical_name!r} is duplicated by task sets "
                    f"{previous!r} and {item.task_set.id!r}"
                )
            seen[logical_name] = item.task_set.id


def _semantic_manifest(
    suite: Suite,
    arms: tuple[CompiledArm, ...],
    task_sets: tuple[CompiledTaskSet, ...],
) -> dict[str, Any]:
    jobs = []
    for item in task_sets:
        semantic_job = {
            "task_set_id": item.task_set.id,
            "arm_ids": [arm.arm.id for arm in arms],
            "attempts": suite.run.attempts,
            "concurrency": suite.run.concurrency,
            "max_retries": suite.run.max_retries,
            "timeout_seconds": suite.run.timeout_seconds,
        }
        jobs.append(
            {
                **semantic_job,
                "semantic_sha256": hashlib.sha256(
                    _canonical_json(semantic_job)
                ).hexdigest(),
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "suite": {"id": suite.id, "title": suite.title},
        "harbor": {
            "version": suite.harbor.version,
            "commit": suite.harbor.commit,
        },
        "task_sets": [_semantic_task_set(item, suite.project_root) for item in task_sets],
        "arms": [
            _semantic_arm(item, suite.run.timeout_seconds)
            for item in arms
        ],
        "run": {
            "attempts": suite.run.attempts,
            "concurrency": suite.run.concurrency,
            "max_retries": suite.run.max_retries,
            "timeout_seconds": suite.run.timeout_seconds,
            "scheduler": "harbor",
        },
        "evidence": {
            "harbor_lock": suite.evidence.harbor_lock,
            "verifier": suite.evidence.verifier,
            "trajectory": suite.evidence.trajectory,
            "usage": suite.evidence.usage,
        },
        "publication": {
            "scope": suite.publication.scope,
            "completeness": suite.publication.completeness,
        },
        "jobs": jobs,
    }


def _semantic_task_set(item: CompiledTaskSet, root: Path) -> dict[str, Any]:
    task_set = item.task_set
    if task_set.kind == "local":
        assert task_set.path is not None
        return {
            "id": task_set.id,
            "kind": "local",
            "path": task_set.path.relative_to(root).as_posix(),
            "content_sha256": item.content_sha256,
            "tasks": [
                {"directory": directory, "logical_name": logical}
                for directory, logical in zip(
                    item.task_names or (), item.logical_names or ()
                )
            ],
        }
    source: dict[str, Any] = {
        "id": task_set.id,
        "kind": "harbor",
        "name": task_set.name,
    }
    if "/" in (task_set.name or ""):
        source["ref"] = task_set.ref
    else:
        source["version"] = task_set.ref
    if task_set.git_commit is not None:
        source["git_commit"] = task_set.git_commit
    if task_set.subdir is not None:
        source["subdir"] = task_set.subdir
    source["tasks"] = None
    return source


def _semantic_arm(
    item: CompiledArm,
    timeout_seconds: float,
) -> dict[str, Any]:
    profile: dict[str, Any]
    if isinstance(item.profile, StockProfileSpec):
        profile = {
            "id": item.profile.id,
            "kind": "stock",
            "harness": item.profile.harness,
        }
    else:
        profile = {
            "id": item.profile.id,
            "kind": "custom",
            "import_path": item.profile.import_path,
            "distribution": item.profile.distribution,
            "version": item.profile.version,
        }
    rendered_agent = _render_agent_config(item, timeout_seconds)
    agent_config_sha256 = hashlib.sha256(
        _canonical_json(rendered_agent)
    ).hexdigest()
    return {
        "id": item.arm.id,
        "harness": item.arm.harness,
        "profile": profile,
        "canonical_model": item.arm.model,
        "agent_config_sha256": agent_config_sha256,
        "agent": {
            "execution_id": item.arm.id,
            **rendered_agent,
        },
    }


def _build_suite_job_artifact(
    base_artifact: HarborJobArtifact,
    arms: tuple[CompiledArm, ...],
    timeout_seconds: float,
) -> HarborJobArtifact:
    """Build final job bytes from the same rendered configs bound in the plan."""

    data = base_artifact.as_dict()
    built_agents = data.get("agents")
    if not isinstance(built_agents, list) or len(built_agents) != len(arms):
        raise SuiteRunError("generated Harbor job agent expansion is inconsistent")
    data["agents"] = [
        _render_agent_config(item, timeout_seconds) for item in arms
    ]
    json_bytes = _canonical_json(data, indent=2)
    return replace(
        base_artifact,
        json_bytes=json_bytes,
        sha256=hashlib.sha256(json_bytes).hexdigest(),
    )


def _render_agent_config(
    item: CompiledArm,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Render the exact secret-free Harbor AgentConfig for one suite arm."""

    agent: dict[str, Any] = {
        "import_path": item.agent.import_path,
        "model_name": item.agent.model_name,
        "override_timeout_sec": timeout_seconds,
    }
    if item.agent.n_concurrent is not None:
        agent["n_concurrent"] = item.agent.n_concurrent
    if item.agent.concurrency_group is not None:
        agent["concurrency_group"] = item.agent.concurrency_group
    if item.agent.kwargs:
        agent["kwargs"] = dict(item.agent.kwargs)
    if item.agent.env:
        agent["env"] = dict(sorted(item.agent.env.items()))
    if item.agent.extra_allowed_hosts:
        agent["extra_allowed_hosts"] = list(item.agent.extra_allowed_hosts)
    return agent


def _assert_semantic_manifest_safe(value: Any, path: str = "manifest") -> None:
    """Reject host paths if a future field accidentally enters the manifest."""

    if isinstance(value, dict):
        for key, child in value.items():
            _assert_semantic_manifest_safe(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_semantic_manifest_safe(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and (
        value.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise SuiteRunError(
            f"{path} contains an absolute or home-relative path"
        )


def _validate_custom_runtime(compiled: CompiledSuite) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    profiles: dict[str, CustomProfileSpec] = {}
    for item in compiled.arms:
        if not isinstance(item.profile, CustomProfileSpec):
            continue
        profiles[item.profile.id] = item.profile
        for _, template in item.profile.env:
            match = _ENV_TEMPLATE_RE.fullmatch(template)
            if match is None:
                raise SuiteRunError(
                    f"custom profile {item.profile.id!r} has invalid env template"
                )
            host_name = match.group(1)
            value = os.environ.get(host_name)
            if value is None:
                raise SuiteRunError(
                    f"custom profile {item.profile.id!r} requires missing "
                    f"host environment variable {host_name}"
                )
            forwarded[host_name] = value
    for profile in profiles.values():
        _verify_custom_distribution(profile)
    return forwarded


def _verify_custom_distribution(profile: CustomProfileSpec) -> None:
    try:
        distribution = metadata.distribution(profile.distribution)
    except metadata.PackageNotFoundError as exc:
        raise SuiteRunError(
            f"custom profile {profile.id!r} requires installed distribution "
            f"{profile.distribution}=={profile.version}"
        ) from exc
    installed = distribution.version
    if installed != profile.version:
        raise SuiteRunError(
            f"custom profile {profile.id!r} requires "
            f"{profile.distribution}=={profile.version}, found {installed}"
        )

    installed_name = distribution.metadata.get("Name")
    if (
        not isinstance(installed_name, str)
        or _canonical_distribution_name(installed_name)
        != _canonical_distribution_name(profile.distribution)
    ):
        raise SuiteRunError(
            f"custom profile {profile.id!r} resolved a different distribution "
            f"identity for {profile.distribution!r}"
        )

    module_path = profile.import_path.split(":", 1)[0].replace(".", "/")
    candidates = {
        f"{module_path}{suffix}" for suffix in all_suffixes()
    } | {
        f"{module_path}/__init__{suffix}" for suffix in all_suffixes()
    }
    files = distribution.files
    matching = [
        file
        for file in (files or ())
        if Path(str(file)).as_posix() in candidates
    ]
    if len(matching) != 1:
        raise SuiteRunError(
            f"custom profile {profile.id!r} import {profile.import_path!r} "
            f"is not uniquely owned by installed distribution "
            f"{profile.distribution!r}"
        )
    installed_module = Path(distribution.locate_file(matching[0]))
    if installed_module.is_symlink() or not installed_module.is_file():
        raise SuiteRunError(
            f"custom profile {profile.id!r} installed module is not a "
            f"regular file: {profile.import_path!r}"
        )


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _stage_stock_credentials(
    stack: ExitStack,
    compiled: CompiledSuite,
    process_env: dict[str, str],
) -> dict[str, HarborOAuthCredential]:
    resolved: dict[str, HarborHarnessProfile] = {}
    for item in compiled.arms:
        if not isinstance(item.profile, StockProfileSpec):
            continue
        current = resolve_harbor_profile(item.profile.harness, item.arm.model)
        previous = resolved.setdefault(item.profile.id, current)
        if previous.auth != current.auth:
            raise SuiteRunError(
                f"stock profile {item.profile.id!r} resolved inconsistent auth"
            )

    oauth: dict[str, HarborOAuthCredential] = {}
    for profile_id, profile in resolved.items():
        if profile.auth.strategy == AUTH_STRATEGY_OAUTH:
            master = _resolve_auth_source(
                profile.harness, profile.auth.source_candidates
            )
            credential = stack.enter_context(HarborOAuthCredential(master))
            oauth[profile_id] = credential
            config = credential.config
            process_env[profile.auth.input_env] = config.auth_json_path
            if profile.auth.return_env is None:
                raise SuiteRunError(
                    f"stock profile {profile_id!r} lacks an auth return environment"
                )
            process_env[profile.auth.return_env] = config.auth_return_path
        else:
            archive = stack.enter_context(
                staged_subscription_auth(
                    profile.harness,
                    profile.auth.source_candidates,
                )
            )
            process_env[profile.auth.input_env] = str(archive)
    return oauth


def _resolve_auth_source(harness: str, candidates: tuple[str, ...]) -> Path:
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and not path.is_symlink():
            return path.resolve()
    raise SuiteRunError(
        f"{harness} credential is unavailable; checked: {', '.join(candidates)}"
    )


def _seed_missing_oauth_returns(
    credentials: dict[str, HarborOAuthCredential],
) -> None:
    for credential in credentials.values():
        config = credential.config
        destination = Path(config.auth_return_path)
        if destination.exists():
            continue
        shutil.copyfile(config.auth_json_path, destination)
        destination.chmod(0o600)


def _completed_harbor_job(job_path: Path) -> bool:
    """Return whether persisted Harbor summary proves no trial remains."""

    result_path = job_path / "result.json"
    if result_path.is_symlink() or not result_path.is_file():
        return False
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(result, dict) or not result.get("finished_at"):
        return False
    total = result.get("n_total_trials")
    stats = result.get("stats")
    return (
        isinstance(total, int)
        and not isinstance(total, bool)
        and total >= 1
        and isinstance(stats, dict)
        and stats.get("n_completed_trials") == total
        and stats.get("n_running_trials") == 0
        and stats.get("n_pending_trials") == 0
    )


def _oauth_return_missing(
    credentials: dict[str, HarborOAuthCredential],
) -> bool:
    return any(
        not Path(credential.config.auth_return_path).is_file()
        for credential in credentials.values()
    )


def _write_manifest(compiled: CompiledSuite) -> Path:
    results_dir = compiled.config.results_dir
    if results_dir is None:
        raise SuiteRunError("results_dir is required")
    destination = (
        Path(results_dir)
        / "suite-runs"
        / f"{compiled.manifest_sha256}.json"
    )
    _ensure_safe_artifact_directory(
        compiled.suite.project_root,
        destination.parent,
        label="suite manifest directory",
    )
    _write_immutable(destination, compiled.manifest_bytes)
    return destination


def _ensure_safe_artifact_directory(
    project_root: Path,
    directory: Path,
    *,
    label: str,
) -> None:
    """Create a project-local directory without following any symlink."""

    root = Path(os.path.abspath(project_root))
    target = Path(os.path.abspath(directory))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise SuiteRunError(f"{label} escapes the project root: {target}") from exc

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(root, flags)
    except OSError as exc:
        raise SuiteRunError(f"{label} has an unsafe project root") from exc
    current = root
    try:
        for part in relative.parts:
            current = current / part
            try:
                child_fd = os.open(part, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, dir_fd=directory_fd)
                    child_fd = os.open(part, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise SuiteRunError(
                        f"{label} has an unsafe component: {current}"
                    ) from exc
            except OSError as exc:
                raise SuiteRunError(
                    f"{label} has an unsafe component: {current}"
                ) from exc
            os.close(directory_fd)
            directory_fd = child_fd
    finally:
        os.close(directory_fd)


def _write_immutable(destination: Path, content: bytes) -> None:
    if destination.is_symlink():
        raise SuiteRunError(f"artifact path must not be a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != content:
            raise SuiteRunError(
                f"refusing to replace different immutable artifact: {destination}"
            )
        return
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != content:
                raise SuiteRunError(
                    f"refusing to replace different immutable artifact: {destination}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json(value: Any, *, indent: int | None = None) -> bytes:
    separators = None if indent is not None else (",", ":")
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=indent,
            separators=separators,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _relative_artifact(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return "<outside-project>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obench run",
        description="Compile or execute a Harbor-native OpenBench suite.",
    )
    parser.add_argument("suite", nargs="?", help="explicit suite.toml path")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="emit the deterministic semantic manifest without execution",
    )
    parser.add_argument(
        "--harbor-binary",
        default="harbor",
        help="Harbor executable name or path (default: harbor)",
    )
    args = parser.parse_args(argv)

    try:
        compiled = compile_suite(args.suite)
        if args.plan:
            plan_jobs(compiled)
            output = {
                "manifest_sha256": compiled.manifest_sha256,
                "manifest": compiled.manifest,
            }
            sys.stdout.write(_canonical_json(output, indent=2).decode("utf-8"))
            return 0
        result = run_suite(compiled, harbor_binary=args.harbor_binary)
    except (
        OSError,
        ProfileSpecError,
        SuiteError,
        SuiteRunError,
        HarborRunError,
        ValueError,
    ) as exc:
        print(f"obench run: {exc}", file=sys.stderr)
        return 1

    root = compiled.suite.project_root
    print(f"suite manifest: {result.manifest_sha256}")
    print(f"manifest artifact: {_relative_artifact(result.manifest_path, root)}")
    for artifact in result.artifacts:
        print(
            f"{artifact.task_set_id}: harbor exit={artifact.returncode} "
            f"config={_relative_artifact(artifact.config_path, root)} "
            f"job={_relative_artifact(artifact.harbor_job_path, root)}"
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
