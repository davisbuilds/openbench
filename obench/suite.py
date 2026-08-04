"""Strict parser and immutable model for Harbor-native ``suite.toml`` files.

The suite is human-authored benchmark intent.  It deliberately excludes
credentials, runtime directories, and generated Harbor job state.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Any


SCHEMA_VERSION = 1

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_HARBOR_NAME_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*\Z"
)
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_DIGEST_REF_RE = re.compile(r"sha256:[0-9a-fA-F]{64}\Z")
_VERSION_REF_RE = re.compile(
    r"v?[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)


class SuiteError(ValueError):
    """A suite file is unsafe, ambiguous, or outside the v1 schema."""


@dataclass(frozen=True)
class HarborPin:
    version: str
    commit: str


@dataclass(frozen=True)
class TaskSet:
    id: str
    kind: str
    path: Path | None = None
    name: str | None = None
    ref: str | None = None
    git_commit: str | None = None
    subdir: str | None = None


@dataclass(frozen=True)
class Arm:
    id: str
    harness: str
    profile: str
    model: str


@dataclass(frozen=True)
class RunPolicy:
    attempts: int
    concurrency: int
    max_retries: int
    timeout_seconds: float


@dataclass(frozen=True)
class EvidenceRequirements:
    harbor_lock: bool
    verifier: bool
    trajectory: bool
    usage: bool


@dataclass(frozen=True)
class PublicationPolicy:
    completeness: str


@dataclass(frozen=True)
class Suite:
    path: Path
    project_root: Path
    schema_version: int
    id: str
    title: str
    harbor: HarborPin
    task_sets: tuple[TaskSet, ...]
    arms: tuple[Arm, ...]
    run: RunPolicy
    evidence: EvidenceRequirements
    publication: PublicationPolicy


def load_suite(
    path: str | os.PathLike[str],
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> Suite:
    """Parse and validate one v1 suite file without importing Harbor."""

    suite_path = Path(path).expanduser()
    if suite_path.is_symlink():
        raise SuiteError(f"suite file must not be a symlink: {suite_path}")
    suite_path = Path(os.path.abspath(suite_path))
    if not suite_path.is_file():
        raise SuiteError(f"suite file does not exist: {suite_path}")

    root = _project_root(suite_path, project_root)
    try:
        with suite_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SuiteError(f"{suite_path}: invalid TOML: {exc}") from exc

    _expect_table(raw, "suite")
    _expect_keys(
        raw,
        {
            "schema_version",
            "id",
            "title",
            "harbor",
            "task_sets",
            "arms",
            "run",
            "evidence",
            "publication",
        },
        "suite",
    )
    schema_version = _integer(raw.get("schema_version"), "schema_version", minimum=1)
    if schema_version != SCHEMA_VERSION:
        raise SuiteError(
            f"schema_version must be {SCHEMA_VERSION}, got {schema_version}"
        )

    suite_id = _identifier(raw.get("id"), "id")
    title = _string(raw.get("title"), "title")
    harbor = _parse_harbor(raw.get("harbor"))
    task_sets = _parse_task_sets(raw.get("task_sets"), root)
    arms = _parse_arms(raw.get("arms"))
    run = _parse_run(raw.get("run"))
    evidence = _parse_evidence(raw.get("evidence"))
    publication = _parse_publication(raw.get("publication"))

    return Suite(
        path=suite_path,
        project_root=root,
        schema_version=schema_version,
        id=suite_id,
        title=title,
        harbor=harbor,
        task_sets=task_sets,
        arms=arms,
        run=run,
        evidence=evidence,
        publication=publication,
    )


def _project_root(
    suite_path: Path,
    explicit: str | os.PathLike[str] | None,
) -> Path:
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if candidate.is_symlink():
            raise SuiteError(f"project root must not be a symlink: {candidate}")
        root = Path(os.path.abspath(candidate))
    else:
        root = suite_path.parent
        for parent in suite_path.parents:
            if parent.name == ".openbench":
                root = parent.parent
                break
    if not root.is_dir():
        raise SuiteError(f"project root is not a directory: {root}")
    return root


def _parse_harbor(value: Any) -> HarborPin:
    table = _expect_table(value, "harbor")
    _expect_keys(table, {"version", "commit"}, "harbor")
    version = _string(table.get("version"), "harbor.version")
    if _VERSION_REF_RE.fullmatch(version) is None:
        raise SuiteError("harbor.version must be an exact semantic version")
    commit = _string(table.get("commit"), "harbor.commit")
    if _COMMIT_RE.fullmatch(commit) is None:
        raise SuiteError("harbor.commit must be an exact 40- or 64-hex commit")
    return HarborPin(version=version, commit=commit.lower())


def _parse_task_sets(value: Any, root: Path) -> tuple[TaskSet, ...]:
    tables = _table_array(value, "task_sets")
    result: list[TaskSet] = []
    seen_ids: set[str] = set()
    seen_sources: set[tuple[str, str, str | None]] = set()
    for index, table in enumerate(tables):
        label = f"task_sets[{index}]"
        _expect_keys(
            table,
            {"id", "kind", "path", "name", "ref", "git_commit", "subdir"},
            label,
            required={"id", "kind"},
        )
        task_set_id = _identifier(table.get("id"), f"{label}.id")
        if task_set_id in seen_ids:
            raise SuiteError(f"duplicate task set id: {task_set_id}")
        seen_ids.add(task_set_id)
        kind = _string(table.get("kind"), f"{label}.kind")

        if kind == "local":
            _require_absent(table, {"name", "ref", "git_commit", "subdir"}, label)
            path = _local_task_set_path(table.get("path"), root, f"{label}.path")
            source_key = ("local", str(path), None)
            item = TaskSet(id=task_set_id, kind=kind, path=path)
        elif kind == "harbor":
            _require_absent(table, {"path"}, label)
            name = _string(table.get("name"), f"{label}.name")
            if _HARBOR_NAME_RE.fullmatch(name) is None:
                raise SuiteError(f"{label}.name is not a valid Harbor name")
            ref = _immutable_ref(table.get("ref"), f"{label}.ref")
            git_commit = table.get("git_commit")
            if git_commit is not None:
                git_commit = _string(git_commit, f"{label}.git_commit")
                if _COMMIT_RE.fullmatch(git_commit) is None:
                    raise SuiteError(
                        f"{label}.git_commit must be an exact 40- or 64-hex commit"
                    )
                git_commit = git_commit.lower()
            subdir = table.get("subdir")
            if subdir is not None:
                if git_commit is None:
                    raise SuiteError(f"{label}.subdir requires git_commit")
                subdir = _safe_relative(subdir, f"{label}.subdir")
            source_key = ("harbor", name, ref)
            item = TaskSet(
                id=task_set_id,
                kind=kind,
                name=name,
                ref=ref,
                git_commit=git_commit,
                subdir=subdir,
            )
        else:
            raise SuiteError(f"{label}.kind must be 'local' or 'harbor'")

        if source_key in seen_sources:
            raise SuiteError(f"duplicate task set source: {task_set_id}")
        seen_sources.add(source_key)
        result.append(item)
    return tuple(result)


def _parse_arms(value: Any) -> tuple[Arm, ...]:
    tables = _table_array(value, "arms")
    result: list[Arm] = []
    seen_ids: set[str] = set()
    seen_arms: set[tuple[str, str, str]] = set()
    for index, table in enumerate(tables):
        label = f"arms[{index}]"
        _expect_keys(table, {"id", "harness", "profile", "model"}, label)
        arm_id = _identifier(table.get("id"), f"{label}.id")
        harness = _identifier(table.get("harness"), f"{label}.harness")
        profile = _identifier(table.get("profile"), f"{label}.profile")
        model = _string(table.get("model"), f"{label}.model")
        if any(char.isspace() for char in model):
            raise SuiteError(f"{label}.model must not contain whitespace")
        if arm_id in seen_ids:
            raise SuiteError(f"duplicate arm id: {arm_id}")
        arm_key = (harness, profile, model)
        if arm_key in seen_arms:
            raise SuiteError(
                "duplicate arm: "
                f"harness={harness}, profile={profile}, model={model}"
            )
        seen_ids.add(arm_id)
        seen_arms.add(arm_key)
        result.append(
            Arm(id=arm_id, harness=harness, profile=profile, model=model)
        )
    return tuple(result)


def _parse_run(value: Any) -> RunPolicy:
    table = _expect_table(value, "run")
    _expect_keys(
        table,
        {"attempts", "concurrency", "max_retries", "timeout_seconds"},
        "run",
    )
    return RunPolicy(
        attempts=_integer(table.get("attempts"), "run.attempts", minimum=1),
        concurrency=_integer(
            table.get("concurrency"), "run.concurrency", minimum=1
        ),
        max_retries=_integer(
            table.get("max_retries"), "run.max_retries", minimum=0
        ),
        timeout_seconds=_positive_number(
            table.get("timeout_seconds"), "run.timeout_seconds"
        ),
    )


def _parse_evidence(value: Any) -> EvidenceRequirements:
    table = _expect_table(value, "evidence")
    fields = {"harbor_lock", "verifier", "trajectory", "usage"}
    _expect_keys(table, fields, "evidence")
    return EvidenceRequirements(
        harbor_lock=_boolean(table.get("harbor_lock"), "evidence.harbor_lock"),
        verifier=_boolean(table.get("verifier"), "evidence.verifier"),
        trajectory=_boolean(table.get("trajectory"), "evidence.trajectory"),
        usage=_boolean(table.get("usage"), "evidence.usage"),
    )


def _parse_publication(value: Any) -> PublicationPolicy:
    table = _expect_table(value, "publication")
    _expect_keys(table, {"completeness"}, "publication")
    completeness = _string(
        table.get("completeness"), "publication.completeness"
    )
    if completeness not in {"complete", "allow_incomplete"}:
        raise SuiteError(
            "publication.completeness must be 'complete' or 'allow_incomplete'"
        )
    return PublicationPolicy(completeness=completeness)


def _local_task_set_path(value: Any, root: Path, label: str) -> Path:
    relative = _safe_relative(value, label)
    lexical = Path(os.path.abspath(root / relative))
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise SuiteError(f"{label} escapes the project root") from exc

    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise SuiteError(f"{label} must not traverse symlinks: {current}")
    if not lexical.is_dir():
        raise SuiteError(f"{label} is not a directory: {lexical}")
    if (lexical / "task.toml").exists():
        raise SuiteError(f"{label} must name a task-set directory, not one task")

    complete = 0
    partial: list[str] = []
    for child in sorted(lexical.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise SuiteError(f"{label} contains a symlink: {child}")
        if not child.is_dir():
            continue
        has_task = (child / "task.toml").is_file()
        has_instruction = (child / "instruction.md").is_file()
        if has_task and has_instruction:
            _reject_tree_symlinks(child, label)
            complete += 1
        elif has_task or has_instruction:
            partial.append(child.name)
    if partial:
        raise SuiteError(
            f"{label} contains partial Harbor tasks: {', '.join(partial)}"
        )
    if complete == 0:
        raise SuiteError(f"{label} contains no Harbor tasks")
    return lexical


def _reject_tree_symlinks(task: Path, label: str) -> None:
    for directory, names, files in os.walk(task, followlinks=False):
        parent = Path(directory)
        for name in (*names, *files):
            candidate = parent / name
            if candidate.is_symlink():
                raise SuiteError(f"{label} contains a symlink: {candidate}")


def _immutable_ref(value: Any, label: str) -> str:
    ref = _string(value, label)
    if (
        _DIGEST_REF_RE.fullmatch(ref) is None
        and _COMMIT_RE.fullmatch(ref) is None
        and _VERSION_REF_RE.fullmatch(ref) is None
    ):
        raise SuiteError(
            f"{label} must be an immutable sha256 digest, exact commit, "
            "or exact semantic version"
        )
    return ref


def _safe_relative(value: Any, label: str) -> str:
    text = _string(value, label)
    path = Path(text)
    if path.is_absolute() or text.startswith("~") or ".." in path.parts:
        raise SuiteError(f"{label} must be a safe relative path")
    if text in {".", ""}:
        raise SuiteError(f"{label} must not be the project root")
    return text


def _expect_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SuiteError(f"{label} must be a TOML table")
    return value


def _table_array(value: Any, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise SuiteError(f"{label} must contain one or more tables")
    tables: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        tables.append(_expect_table(item, f"{label}[{index}]"))
    return tuple(tables)


def _expect_keys(
    table: dict[str, Any],
    allowed: set[str],
    label: str,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise SuiteError(f"{label} has unknown keys: {', '.join(unknown)}")
    missing = sorted((allowed if required is None else required) - set(table))
    if missing:
        raise SuiteError(f"{label} is missing required keys: {', '.join(missing)}")


def _require_absent(
    table: dict[str, Any],
    fields: set[str],
    label: str,
) -> None:
    present = sorted(fields & set(table))
    if present:
        raise SuiteError(
            f"{label} has fields invalid for its kind: {', '.join(present)}"
        )


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SuiteError(f"{label} must be a non-empty string")
    if value != value.strip() or "\x00" in value:
        raise SuiteError(f"{label} contains unsafe whitespace or NUL")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _string(value, label)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise SuiteError(
            f"{label} must start with an alphanumeric and contain only "
            "letters, digits, dot, underscore, or hyphen"
        )
    return text


def _integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SuiteError(f"{label} must be an integer")
    if value < minimum:
        raise SuiteError(f"{label} must be at least {minimum}")
    return value


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SuiteError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SuiteError(f"{label} must be a positive finite number")
    return number


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise SuiteError(f"{label} must be a boolean")
    return value
