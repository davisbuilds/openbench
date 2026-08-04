"""Deterministic Harbor 0.20.0 native job configuration authoring.

This module intentionally does not import Harbor. It emits the JSON schema
accepted by ``harbor run -c`` and leaves execution, locking, retries, and
resume reconciliation to Harbor itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


HARBOR_VERSION = "0.20.0"
HARBOR_GIT_COMMIT = "72bc40b1e58b47a9cc6e0f14c29aced3a9e53767"
HARBOR_JOB_CONFIG_SOURCE = (
    "https://github.com/harbor-framework/harbor/blob/"
    f"{HARBOR_GIT_COMMIT}/src/harbor/models/job/config.py"
)
COMPARISON_PLAN_SCHEMA_VERSION = "openbench-harbor-comparison-plan-v2"
COMPARISON_PLAN_SUFFIX = ".openbench-comparison-plan.json"

DEFAULT_RETRY_EXCLUSIONS = (
    "AgentAuthenticationError",
    "AgentSafetyRefusalError",
    "AgentTimeoutError",
    "ModelNotFoundError",
    "RewardFileEmptyError",
    "RewardFileNotFoundError",
    "VerifierOutputParseError",
    "VerifierTimeoutError",
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(auth|credential|key|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)


class HarborJobError(ValueError):
    """Raised when a job cannot be represented safely and unambiguously."""


@dataclass(frozen=True)
class LocalTaskSet:
    """A local directory containing exported Harbor task directories."""

    path: str | os.PathLike[str]
    task_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Dataset:
    """An immutable Harbor registry or package dataset reference."""

    name: str
    version: str | None = None
    ref: str | None = None
    task_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class AgentProfile:
    """One harness profile, optionally bound to an exact model identity.

    ``env`` values must be Harbor host-environment templates such as
    ``${OPENAI_API_KEY}``. Literal runtime values are intentionally rejected.
    """

    profile_id: str
    arm_id: str | None = None
    canonical_harness: str | None = None
    canonical_model: str | None = None
    model_name: str | None = None
    name: str | None = None
    import_path: str | None = None
    override_timeout_sec: float | None = None
    n_concurrent: int | None = None
    concurrency_group: str | None = None
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    extra_allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConcurrencyPolicy:
    n_concurrent_trials: int


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    include_exceptions: tuple[str, ...] | None = None
    exclude_exceptions: tuple[str, ...] = DEFAULT_RETRY_EXCLUSIONS
    wait_multiplier: float = 1.0
    min_wait_sec: float = 1.0
    max_wait_sec: float = 60.0


@dataclass(frozen=True)
class HarborJobSpec:
    """Complete OpenBench-authored Harbor job matrix."""

    job_name: str
    jobs_dir: str | os.PathLike[str]
    source: LocalTaskSet | Dataset
    agent_profiles: tuple[AgentProfile, ...]
    models: tuple[str, ...]
    attempts: int
    concurrency: ConcurrencyPolicy
    retry: RetryPolicy


@dataclass(frozen=True)
class HarborComparisonPlanArtifact:
    """OpenBench comparison coordinates bound to one native Harbor job config."""

    json_bytes: bytes
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.json_bytes)


@dataclass(frozen=True)
class HarborJobArtifact:
    """Canonical publishable config bytes and their immutable identity."""

    json_bytes: bytes
    sha256: str
    job_name: str
    jobs_dir: Path
    trial_count: int | None
    source_task_count: int | None
    comparison_plan: HarborComparisonPlanArtifact | None

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.json_bytes)


@dataclass(frozen=True)
class HarborCommandPlan:
    """A single native Harbor command; Harbor remains the scheduler."""

    argv: tuple[str, ...]
    config_path: Path
    config_sha256: str
    expected_job_path: Path
    resumes_existing_job: bool
    harbor_version: str = HARBOR_VERSION


def build_job_config(spec: HarborJobSpec) -> HarborJobArtifact:
    """Validate ``spec`` and render deterministic Harbor JobConfig JSON."""

    job_name = _validate_identifier(spec.job_name, "job_name")
    jobs_dir = _normalize_output_dir(spec.jobs_dir)
    if not isinstance(spec.attempts, int) or isinstance(spec.attempts, bool):
        raise HarborJobError("attempts must be an integer")
    if spec.attempts < 1:
        raise HarborJobError("attempts must be at least 1")

    models = (
        _validate_unique_strings(spec.models, "models")
        if spec.models
        else ()
    )
    profiles = _validate_profiles(spec.agent_profiles, spec.concurrency)
    retry = _render_retry(spec.retry)
    (
        source,
        source_task_count,
        source_task_names,
        dataset_descriptor,
    ) = _render_source(spec.source)

    agents = []
    comparison_arms = []
    comparison_agent_config_digests: set[str] = set()
    comparison_arm_ids: set[str] = set()
    for profile in profiles:
        profile_models = (
            (profile.model_name,)
            if profile.model_name is not None
            else models
        )
        if not profile_models:
            raise HarborJobError(
                f"profile {profile.profile_id} requires model_name when "
                "the job has no shared models"
            )
        if profile.arm_id is not None and len(profile_models) != 1:
            raise HarborJobError(
                f"profile {profile.profile_id} arm_id requires exactly one model"
            )
        for model in profile_models:
            rendered_agent = _render_agent(profile, model)
            agent_config_name = (
                rendered_agent.get("name") or rendered_agent["import_path"]
            )
            agent_config_sha256 = canonical_agent_config_sha256(rendered_agent)
            if agent_config_sha256 in comparison_agent_config_digests:
                raise HarborJobError(
                    "comparison arms must have distinct rendered agent configs: "
                    f"{profile.profile_id}"
                )
            comparison_agent_config_digests.add(agent_config_sha256)
            arm_id = (
                profile.arm_id or profile.profile_id
                if len(profile_models) == 1
                else f"{profile.profile_id}@{model}"
            )
            if arm_id in comparison_arm_ids:
                raise HarborJobError(
                    f"comparison arm identity is ambiguous: {arm_id}"
                )
            comparison_arm_ids.add(arm_id)
            agents.append(rendered_agent)
            comparison_arms.append({
                "arm_id": arm_id,
                "agent_config_name": agent_config_name,
                "harbor_model_name": model,
                "agent_config_sha256": agent_config_sha256,
                "canonical_harness": (
                    profile.canonical_harness or profile.profile_id
                ),
                "canonical_model": profile.canonical_model or model,
            })
    config = {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": spec.attempts,
        "n_concurrent_trials": spec.concurrency.n_concurrent_trials,
        "retry": retry,
        "agents": agents,
        **source,
    }
    json_bytes = (
        json.dumps(
            config,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    trial_count = (
        source_task_count * len(agents) * spec.attempts
        if source_task_count is not None
        else None
    )
    config_sha256 = hashlib.sha256(json_bytes).hexdigest()
    comparison_plan = _build_comparison_plan(
        job_name=job_name,
        job_config_sha256=config_sha256,
        dataset=dataset_descriptor,
        tasks=source_task_names,
        arms=comparison_arms,
        attempts=spec.attempts,
    )
    return HarborJobArtifact(
        json_bytes=json_bytes,
        sha256=config_sha256,
        job_name=job_name,
        jobs_dir=jobs_dir,
        trial_count=trial_count,
        source_task_count=source_task_count,
        comparison_plan=comparison_plan,
    )


def _build_comparison_plan(
    *,
    job_name: str,
    job_config_sha256: str,
    dataset: dict[str, Any] | None,
    tasks: tuple[str, ...] | None,
    arms: list[dict[str, str]],
    attempts: int,
) -> HarborComparisonPlanArtifact:
    value = {
        "schema_version": COMPARISON_PLAN_SCHEMA_VERSION,
        "harbor_version": HARBOR_VERSION,
        "harbor_git_commit_hash": HARBOR_GIT_COMMIT,
        "job_name": job_name,
        "job_config_sha256": job_config_sha256,
        "attempts": attempts,
        "dataset": dataset,
        "tasks": None if tasks is None else list(tasks),
        "arms": arms,
    }
    json_bytes = canonical_comparison_plan_bytes(value)
    return HarborComparisonPlanArtifact(
        json_bytes=json_bytes,
        sha256=hashlib.sha256(json_bytes).hexdigest(),
    )


def canonical_comparison_plan_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize a comparison plan in its single supported canonical form."""

    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_agent_config_sha256(value: Mapping[str, Any]) -> str:
    """Hash one exact secret-free rendered Harbor AgentConfig mapping."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def comparison_plan_path_for_config(
    config_path: str | os.PathLike[str],
) -> Path:
    """Return the deterministic OpenBench sidecar path for a Harbor config."""

    path = Path(config_path).expanduser()
    return path.with_name(path.stem + COMPARISON_PLAN_SUFFIX)


def write_comparison_plan(
    artifact: HarborComparisonPlanArtifact,
    path: str | os.PathLike[str],
) -> Path:
    """Atomically create a sidecar, allowing only identical existing bytes."""

    return _write_immutable_bytes(
        artifact.json_bytes,
        path,
        path_label="comparison plan path",
        artifact_label="Harbor comparison plan",
    )


def write_job_config(
    artifact: HarborJobArtifact, path: str | os.PathLike[str]
) -> Path:
    """Atomically create a config, allowing only an identical existing file."""

    return _write_immutable_bytes(
        artifact.json_bytes,
        path,
        path_label="config path",
        artifact_label="Harbor job config",
    )


def _write_immutable_bytes(
    payload: bytes,
    path: str | os.PathLike[str],
    *,
    path_label: str,
    artifact_label: str,
) -> Path:
    destination = Path(path).expanduser()
    if destination.suffix.lower() != ".json":
        raise HarborJobError(f"{artifact_label} path must end in .json")
    if destination.is_symlink():
        raise HarborJobError(f"{path_label} must not be a symlink: {destination}")
    destination = destination.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file():
            raise HarborJobError(
                f"{path_label} is not a regular file: {destination}"
            )
        if destination.read_bytes() != payload:
            raise HarborJobError(
                f"refusing to overwrite different {artifact_label}: {destination}"
            )
        return destination

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise HarborJobError(
                    f"refusing to overwrite different {artifact_label}: {destination}"
                ) from None
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def build_command_plan(
    artifact: HarborJobArtifact,
    config_path: str | os.PathLike[str],
    *,
    harbor_binary: str | os.PathLike[str] = "harbor",
) -> HarborCommandPlan:
    """Plan ``harbor run -c`` after binding to exact config bytes.

    If the expected job directory already contains Harbor's ``config.json``,
    the same command is a resume candidate. Harbor remains responsible for
    reconciling its config, lock, results, and incomplete trials.
    """

    path = Path(config_path).expanduser()
    if path.is_symlink():
        raise HarborJobError(f"config path must not be a symlink: {path}")
    path = path.resolve(strict=False)
    if not path.is_file():
        raise HarborJobError(f"Harbor job config does not exist: {path}")
    actual = path.read_bytes()
    if actual != artifact.json_bytes:
        raise HarborJobError(
            "Harbor job config content does not match the planned artifact digest"
        )

    binary = os.fspath(harbor_binary)
    if not binary or "\x00" in binary:
        raise HarborJobError("harbor_binary must be a non-empty command or path")

    expected_job_path = artifact.jobs_dir / artifact.job_name
    resumes = _validate_resume_path(expected_job_path)
    return HarborCommandPlan(
        argv=(binary, "run", "-c", str(path)),
        config_path=path,
        config_sha256=artifact.sha256,
        expected_job_path=expected_job_path,
        resumes_existing_job=resumes,
    )


def _validate_resume_path(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_dir():
        raise HarborJobError(f"expected Harbor job path is unsafe: {path}")
    config_path = path / "config.json"
    lock_path = path / "lock.json"
    if config_path.is_symlink() or lock_path.is_symlink():
        raise HarborJobError(f"Harbor job state files must not be symlinks: {path}")
    return config_path.is_file()


def _normalize_output_dir(value: str | os.PathLike[str]) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise HarborJobError(f"jobs_dir must not be a symlink: {raw}")
    absolute = Path(os.path.abspath(raw))
    if absolute.exists() and not absolute.is_dir():
        raise HarborJobError(f"jobs_dir must be a directory: {absolute}")
    return absolute


def _render_source(
    source: LocalTaskSet | Dataset,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    int | None,
    tuple[str, ...] | None,
    dict[str, Any] | None,
]:
    if isinstance(source, LocalTaskSet):
        dataset, task_names = _render_local_task_set(source)
        return (
            {"datasets": [dataset], "tasks": []},
            len(task_names),
            task_names,
            None,
        )
    if isinstance(source, Dataset):
        dataset = _render_dataset(source)
        return {"datasets": [dataset], "tasks": []}, None, None, dataset
    raise HarborJobError("source must be exactly one LocalTaskSet or Dataset")


def _render_local_task_set(
    source: LocalTaskSet,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    raw = Path(source.path).expanduser()
    if raw.is_symlink():
        raise HarborJobError(f"local task-set path must not be a symlink: {raw}")
    root = raw.resolve(strict=False)
    if not root.is_dir():
        raise HarborJobError(f"local task-set path is not a directory: {root}")
    if (root / "task.toml").exists():
        raise HarborJobError(
            "local task-set path points to one task; pass its exported parent dataset"
        )

    discovered: list[str] = []
    partial: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise HarborJobError(
                f"local task-set children must not be symlinks: {child}"
            )
        if not child.is_dir():
            continue
        has_config = (child / "task.toml").is_file()
        has_instruction = (child / "instruction.md").is_file()
        if has_config and has_instruction:
            discovered.append(child.name)
        elif has_config or has_instruction:
            partial.append(child.name)
    if partial:
        raise HarborJobError(
            "local task-set contains partial Harbor tasks: " + ", ".join(partial)
        )
    if not discovered:
        raise HarborJobError(f"local task-set contains no Harbor tasks: {root}")

    if source.task_names is None:
        selected = tuple(discovered)
    else:
        selected = _validate_unique_strings(source.task_names, "task_names")
        unavailable = sorted(set(selected) - set(discovered))
        if unavailable:
            raise HarborJobError(
                "selected tasks are not complete children of the task set: "
                + ", ".join(unavailable)
            )
        selected = tuple(sorted(selected))
    return {"path": str(root), "task_names": list(selected)}, selected


def _render_dataset(source: Dataset) -> dict[str, Any]:
    name = _validate_nonempty(source.name, "dataset name")
    if any(char.isspace() for char in name):
        raise HarborJobError("dataset name must not contain whitespace")
    package = "/" in name
    if package:
        if source.ref is None or source.version is not None:
            raise HarborJobError(
                "package datasets require ref and must not define version"
            )
    elif source.version is None or source.ref is not None:
        raise HarborJobError(
            "registry datasets require version and must not define ref"
        )

    rendered: dict[str, Any] = {"name": name}
    if source.version is not None:
        rendered["version"] = _validate_nonempty(source.version, "dataset version")
    if source.ref is not None:
        ref = _validate_nonempty(source.ref, "dataset ref")
        if package and re.fullmatch(r"sha256:[0-9a-f]{64}", ref) is None:
            raise HarborJobError(
                "package dataset ref must be an immutable sha256 digest"
            )
        rendered["ref"] = ref
    if source.task_names is not None:
        rendered["task_names"] = list(
            _validate_unique_strings(source.task_names, "task_names")
        )
    return rendered


def _validate_profiles(
    profiles: tuple[AgentProfile, ...], concurrency: ConcurrencyPolicy
) -> tuple[AgentProfile, ...]:
    if not isinstance(concurrency.n_concurrent_trials, int) or isinstance(
        concurrency.n_concurrent_trials, bool
    ):
        raise HarborJobError("n_concurrent_trials must be an integer")
    if concurrency.n_concurrent_trials < 1:
        raise HarborJobError("n_concurrent_trials must be at least 1")
    if not profiles:
        raise HarborJobError("agent_profiles must not be empty")

    seen: set[str] = set()
    group_limits: dict[str, int] = {}
    for profile in profiles:
        profile_id = _validate_identifier(profile.profile_id, "profile_id")
        if profile_id in seen:
            raise HarborJobError(f"duplicate profile_id: {profile_id}")
        seen.add(profile_id)
        if (profile.name is None) == (profile.import_path is None):
            raise HarborJobError(
                f"profile {profile_id} must define exactly one of name or import_path"
            )
        if profile.name is not None:
            _validate_nonempty(profile.name, f"profile {profile_id} name")
        if profile.import_path is not None:
            import_path = _validate_nonempty(
                profile.import_path, f"profile {profile_id} import_path"
            )
            if ":" not in import_path:
                raise HarborJobError(
                    f"profile {profile_id} import_path must use module:Class form"
                )
        if profile.model_name is not None:
            _validate_nonempty(
                profile.model_name, f"profile {profile_id} model_name"
            )
        if profile.arm_id is not None:
            _validate_identifier(profile.arm_id, f"profile {profile_id} arm_id")
        if profile.canonical_harness is not None:
            _validate_nonempty(
                profile.canonical_harness,
                f"profile {profile_id} canonical_harness",
            )
        if profile.canonical_model is not None:
            _validate_nonempty(
                profile.canonical_model,
                f"profile {profile_id} canonical_model",
            )
        if profile.override_timeout_sec is not None:
            _validate_positive_number(
                profile.override_timeout_sec,
                f"profile {profile_id} override_timeout_sec",
            )
        if profile.n_concurrent is not None:
            if not isinstance(profile.n_concurrent, int) or isinstance(
                profile.n_concurrent, bool
            ):
                raise HarborJobError(
                    f"profile {profile_id} n_concurrent must be an integer"
                )
            if not 1 <= profile.n_concurrent <= concurrency.n_concurrent_trials:
                raise HarborJobError(
                    f"profile {profile_id} n_concurrent must be between 1 and "
                    f"{concurrency.n_concurrent_trials}"
                )
        if profile.concurrency_group is not None:
            group = _validate_identifier(
                profile.concurrency_group, f"profile {profile_id} concurrency_group"
            )
            if profile.n_concurrent is None:
                raise HarborJobError(
                    f"profile {profile_id} concurrency_group requires n_concurrent"
                )
            previous = group_limits.setdefault(group, profile.n_concurrent)
            if previous != profile.n_concurrent:
                raise HarborJobError(
                    f"concurrency_group {group} has conflicting limits"
                )
        _validate_json_value(profile.kwargs, f"profile {profile_id} kwargs")
        _validate_runtime_env(profile.env, profile_id)
        if profile.extra_allowed_hosts:
            _validate_unique_strings(
                profile.extra_allowed_hosts, "extra_allowed_hosts"
            )
    return profiles


def _render_agent(profile: AgentProfile, model: str) -> dict[str, Any]:
    rendered: dict[str, Any] = {"model_name": model}
    if profile.name is not None:
        rendered["name"] = profile.name
    else:
        rendered["import_path"] = profile.import_path
    if profile.override_timeout_sec is not None:
        rendered["override_timeout_sec"] = profile.override_timeout_sec
    if profile.n_concurrent is not None:
        rendered["n_concurrent"] = profile.n_concurrent
    if profile.concurrency_group is not None:
        rendered["concurrency_group"] = profile.concurrency_group
    if profile.kwargs:
        rendered["kwargs"] = _json_copy(profile.kwargs)
    if profile.env:
        rendered["env"] = dict(sorted(profile.env.items()))
    if profile.extra_allowed_hosts:
        rendered["extra_allowed_hosts"] = list(profile.extra_allowed_hosts)
    return rendered


def _render_retry(policy: RetryPolicy) -> dict[str, Any]:
    if not isinstance(policy.max_retries, int) or isinstance(policy.max_retries, bool):
        raise HarborJobError("max_retries must be an integer")
    if policy.max_retries < 0:
        raise HarborJobError("max_retries must not be negative")
    include = None
    if policy.include_exceptions is not None:
        include = _validate_unique_strings(
            policy.include_exceptions, "include_exceptions", allow_empty=True
        )
    exclude = _validate_unique_strings(
        policy.exclude_exceptions, "exclude_exceptions", allow_empty=True
    )
    if include is not None and set(include) & set(exclude):
        raise HarborJobError(
            "retry include_exceptions and exclude_exceptions must not overlap"
        )
    wait_multiplier = _validate_positive_number(
        policy.wait_multiplier, "wait_multiplier"
    )
    min_wait = _validate_nonnegative_number(policy.min_wait_sec, "min_wait_sec")
    max_wait = _validate_nonnegative_number(policy.max_wait_sec, "max_wait_sec")
    if min_wait > max_wait:
        raise HarborJobError("min_wait_sec must not exceed max_wait_sec")
    return {
        "max_retries": policy.max_retries,
        "include_exceptions": None if include is None else list(sorted(include)),
        "exclude_exceptions": list(sorted(exclude)),
        "wait_multiplier": wait_multiplier,
        "min_wait_sec": min_wait,
        "max_wait_sec": max_wait,
    }


def _validate_runtime_env(env: Mapping[str, str], profile_id: str) -> None:
    for key, value in env.items():
        if not isinstance(key, str) or _ENV_NAME_RE.fullmatch(key) is None:
            raise HarborJobError(f"profile {profile_id} has invalid env key: {key!r}")
        if not isinstance(value, str) or _ENV_TEMPLATE_RE.fullmatch(value) is None:
            raise HarborJobError(
                f"profile {profile_id} env {key} must be a ${{HOST_ENV}} template"
            )


def _validate_json_value(value: Any, label: str) -> None:
    def visit(item: Any, path: str) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise HarborJobError(f"{path} contains a non-finite number")
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise HarborJobError(f"{path} contains a non-string key")
                if _SENSITIVE_KEY_RE.search(key):
                    raise HarborJobError(
                        f"{path}.{key} looks sensitive; use profile env hooks"
                    )
                visit(child, f"{path}.{key}")
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        raise HarborJobError(f"{path} is not a JSON value")

    visit(value, label)


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _validate_identifier(value: str, label: str) -> str:
    value = _validate_nonempty(value, label)
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise HarborJobError(
            f"{label} must start with an alphanumeric and contain only "
            "letters, digits, dot, underscore, or hyphen"
        )
    return value


def _validate_nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarborJobError(f"{label} must be a non-empty string")
    if value != value.strip() or "\x00" in value:
        raise HarborJobError(f"{label} contains unsafe whitespace or NUL")
    return value


def _validate_unique_strings(
    values: Any, label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise HarborJobError(f"{label} must be a sequence of strings")
    try:
        result = tuple(_validate_nonempty(value, label) for value in values)
    except TypeError as exc:
        raise HarborJobError(f"{label} must be a sequence of strings") from exc
    if not result and not allow_empty:
        raise HarborJobError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise HarborJobError(f"{label} must not contain duplicates")
    return result


def _validate_positive_number(value: Any, label: str) -> float:
    number = _validate_nonnegative_number(value, label)
    if number <= 0:
        raise HarborJobError(f"{label} must be greater than zero")
    return number


def _validate_nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarborJobError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise HarborJobError(f"{label} must be a non-negative finite number")
    return number
