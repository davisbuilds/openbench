"""Strict profile specs for Harbor-native OpenBench suite arms."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Any, TypeAlias

from .harbor_job import AgentProfile
from .harbor_profiles import HarborProfileError, resolve_harbor_profile


SCHEMA_VERSION = 1

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_IMPORT_PATH_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
)
_EXACT_VERSION_RE = re.compile(
    r"v?[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_DNS_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])"
    r"(?:auth|bearer|cookie|credential|key|password|secret|session|token)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)
_STOCK_HARNESSES = frozenset({"codex", "pi", "opencode", "cursor", "devin"})


class ProfileSpecError(ValueError):
    """A profile spec is unsafe, ambiguous, or outside the v1 schema."""


@dataclass(frozen=True)
class StockProfileSpec:
    path: Path
    project_root: Path
    schema_version: int
    id: str
    kind: str
    harness: str


@dataclass(frozen=True)
class CustomProfileSpec:
    path: Path
    project_root: Path
    schema_version: int
    id: str
    kind: str
    import_path: str
    version: str
    models: tuple[tuple[str, str], ...]
    env: tuple[tuple[str, str], ...]
    kwargs_json: str
    concurrency_group: str | None
    concurrency_limit: int | None
    extra_allowed_hosts: tuple[str, ...]

    def kwargs(self) -> dict[str, Any]:
        """Return a fresh JSON-safe kwargs mapping."""

        return json.loads(self.kwargs_json)


ProfileSpec: TypeAlias = StockProfileSpec | CustomProfileSpec


@dataclass(frozen=True)
class ProfileRegistry:
    project_root: Path
    profiles: tuple[ProfileSpec, ...]

    def get(self, profile_id: str) -> ProfileSpec:
        matches = tuple(profile for profile in self.profiles if profile.id == profile_id)
        if len(matches) != 1:
            raise ProfileSpecError(
                f"profile id {profile_id!r} does not resolve to exactly one profile"
            )
        return matches[0]


def load_profile(
    path: str | os.PathLike[str],
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> ProfileSpec:
    """Load one ``.openbench/profiles/<id>.toml`` profile."""

    profile_path, root = _profile_path(path, project_root)
    try:
        with profile_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileSpecError(f"{profile_path}: invalid TOML: {exc}") from exc

    table = _expect_table(raw, "profile")
    _expect_keys(
        table,
        {
            "schema_version",
            "id",
            "kind",
            "harness",
            "import_path",
            "version",
            "models",
            "env",
            "kwargs",
            "concurrency_group",
            "concurrency_limit",
            "extra_allowed_hosts",
        },
        "profile",
        required={"schema_version", "id", "kind"},
    )
    schema_version = _integer(
        table.get("schema_version"), "schema_version", minimum=1
    )
    if schema_version != SCHEMA_VERSION:
        raise ProfileSpecError(
            f"schema_version must be {SCHEMA_VERSION}, got {schema_version}"
        )
    profile_id = _identifier(table.get("id"), "id")
    if profile_path.stem != profile_id:
        raise ProfileSpecError(
            f"profile id {profile_id!r} must match filename {profile_path.name!r}"
        )
    kind = _string(table.get("kind"), "kind")

    if kind == "stock":
        _expect_keys(
            table,
            {"schema_version", "id", "kind", "harness"},
            "stock profile",
        )
        harness = _identifier(table.get("harness"), "harness")
        if harness not in _STOCK_HARNESSES:
            raise ProfileSpecError(
                f"unsupported stock harness {harness!r}; "
                f"expected one of {sorted(_STOCK_HARNESSES)}"
            )
        return StockProfileSpec(
            path=profile_path,
            project_root=root,
            schema_version=schema_version,
            id=profile_id,
            kind=kind,
            harness=harness,
        )

    if kind == "custom":
        _expect_keys(
            table,
            {
                "schema_version",
                "id",
                "kind",
                "import_path",
                "version",
                "models",
                "env",
                "kwargs",
                "concurrency_group",
                "concurrency_limit",
                "extra_allowed_hosts",
            },
            "custom profile",
            required={
                "schema_version",
                "id",
                "kind",
                "import_path",
                "version",
                "models",
                "extra_allowed_hosts",
            },
        )
        import_path = _string(table.get("import_path"), "import_path")
        if _IMPORT_PATH_RE.fullmatch(import_path) is None:
            raise ProfileSpecError(
                "import_path must be an exact Python module:Class path"
            )
        version = _string(table.get("version"), "version")
        if _EXACT_VERSION_RE.fullmatch(version) is None:
            raise ProfileSpecError("version must be an exact semantic version")
        models = _model_map(table.get("models"))
        env = _env_map(table.get("env", {}))
        kwargs_json = _kwargs_json(table.get("kwargs", {}))
        concurrency_group, concurrency_limit = _concurrency(table)
        extra_allowed_hosts = _allowed_hosts(table.get("extra_allowed_hosts"))
        return CustomProfileSpec(
            path=profile_path,
            project_root=root,
            schema_version=schema_version,
            id=profile_id,
            kind=kind,
            import_path=import_path,
            version=version,
            models=models,
            env=env,
            kwargs_json=kwargs_json,
            concurrency_group=concurrency_group,
            concurrency_limit=concurrency_limit,
            extra_allowed_hosts=extra_allowed_hosts,
        )

    raise ProfileSpecError("kind must be 'stock' or 'custom'")


def load_profile_registry(
    project_root: str | os.PathLike[str],
) -> ProfileRegistry:
    """Load all profile specs and reject duplicate execution identities."""

    root = _project_root(project_root)
    directory = root / ".openbench" / "profiles"
    _reject_symlink(directory, "profile directory")
    if not directory.is_dir():
        raise ProfileSpecError(f"profile directory does not exist: {directory}")

    paths = sorted(directory.glob("*.toml"), key=lambda item: item.name)
    if not paths:
        raise ProfileSpecError(f"profile directory contains no TOML profiles: {directory}")
    profiles = tuple(load_profile(path, project_root=root) for path in paths)

    seen_ids: set[str] = set()
    seen_identities: dict[tuple[Any, ...], str] = {}
    for profile in profiles:
        if profile.id in seen_ids:
            raise ProfileSpecError(f"duplicate profile id: {profile.id}")
        seen_ids.add(profile.id)
        identity = _execution_identity(profile)
        previous = seen_identities.setdefault(identity, profile.id)
        if previous != profile.id:
            raise ProfileSpecError(
                f"profiles {previous!r} and {profile.id!r} have duplicate "
                "execution identities"
            )
    return ProfileRegistry(project_root=root, profiles=profiles)


def compile_profile(profile: ProfileSpec, canonical_model: str) -> AgentProfile:
    """Compile one parsed profile/model arm into a strict Harbor job profile."""

    model = _model_name(canonical_model, "canonical_model")
    if isinstance(profile, StockProfileSpec):
        try:
            resolved = resolve_harbor_profile(profile.harness, model)
        except HarborProfileError as exc:
            raise ProfileSpecError(str(exc)) from exc
        env = dict(resolved.agent_env)
        env[resolved.auth.input_env] = f"${{{resolved.auth.input_env}}}"
        if resolved.auth.persist_back:
            if resolved.auth.return_env is None:
                raise ProfileSpecError(
                    f"stock profile {profile.id!r} has ambiguous auth return policy"
                )
            env[resolved.auth.return_env] = f"${{{resolved.auth.return_env}}}"
        return AgentProfile(
            profile_id=profile.id,
            model_name=resolved.harbor_model_name,
            import_path=resolved.agent_import_path,
            n_concurrent=resolved.auth.max_concurrent_uses,
            concurrency_group=resolved.auth.concurrency_group,
            kwargs=resolved.agent_kwargs(),
            env=env,
        )

    model_map = dict(profile.models)
    harbor_model = model_map.get(model)
    if harbor_model is None:
        raise ProfileSpecError(
            f"custom profile {profile.id!r} does not support canonical model {model!r}"
        )
    kwargs = profile.kwargs()
    kwargs["version"] = profile.version
    return AgentProfile(
        profile_id=profile.id,
        model_name=harbor_model,
        import_path=profile.import_path,
        n_concurrent=profile.concurrency_limit,
        concurrency_group=profile.concurrency_group,
        kwargs=kwargs,
        env=dict(profile.env),
        extra_allowed_hosts=profile.extra_allowed_hosts,
    )


def _profile_path(
    path: str | os.PathLike[str],
    explicit_root: str | os.PathLike[str] | None,
) -> tuple[Path, Path]:
    raw = Path(path).expanduser()
    _reject_symlink(raw, "profile file")
    profile_path = Path(os.path.abspath(raw))
    if profile_path.suffix != ".toml":
        raise ProfileSpecError("profile file must end in .toml")

    if explicit_root is None:
        if (
            profile_path.parent.name != "profiles"
            or profile_path.parent.parent.name != ".openbench"
        ):
            raise ProfileSpecError(
                "profile file must be under .openbench/profiles"
            )
        root = profile_path.parent.parent.parent
        _reject_symlink(root, "project root")
    else:
        root = _project_root(explicit_root)

    expected = root / ".openbench" / "profiles"
    _reject_symlink(root / ".openbench", ".openbench directory")
    _reject_symlink(expected, "profile directory")
    if profile_path.parent != expected:
        raise ProfileSpecError(
            f"profile file escapes {expected}: {profile_path}"
        )
    if not profile_path.is_file():
        raise ProfileSpecError(f"profile file does not exist: {profile_path}")
    return profile_path, root


def _project_root(value: str | os.PathLike[str]) -> Path:
    raw = Path(value).expanduser()
    _reject_symlink(raw, "project root")
    root = Path(os.path.abspath(raw))
    if not root.is_dir():
        raise ProfileSpecError(f"project root is not a directory: {root}")
    return root


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ProfileSpecError(f"{label} must not be a symlink: {path}")


def _model_map(value: Any) -> tuple[tuple[str, str], ...]:
    table = _expect_table(value, "models")
    if not table:
        raise ProfileSpecError("models must not be empty")
    result: list[tuple[str, str]] = []
    seen_harbor_models: dict[str, str] = {}
    for canonical, harbor in sorted(table.items()):
        canonical_model = _model_name(canonical, "models canonical model")
        harbor_model = _model_name(harbor, f"models.{canonical}")
        previous = seen_harbor_models.setdefault(harbor_model, canonical_model)
        if previous != canonical_model:
            raise ProfileSpecError(
                f"canonical models {previous!r} and {canonical_model!r} map "
                f"to the same Harbor model {harbor_model!r}"
            )
        result.append((canonical_model, harbor_model))
    return tuple(result)


def _env_map(value: Any) -> tuple[tuple[str, str], ...]:
    table = _expect_table(value, "env")
    result: list[tuple[str, str]] = []
    for key, template in sorted(table.items()):
        if _ENV_NAME_RE.fullmatch(key) is None:
            raise ProfileSpecError(f"env has invalid variable name: {key!r}")
        if not isinstance(template, str) or _ENV_TEMPLATE_RE.fullmatch(template) is None:
            raise ProfileSpecError(f"env {key} must be a literal ${{HOST_ENV}} template")
        result.append((key, template))
    return tuple(result)


def _kwargs_json(value: Any) -> str:
    table = _expect_table(value, "kwargs")
    if "version" in table:
        raise ProfileSpecError("kwargs.version is reserved by the profile schema")

    def visit(item: Any, path: str) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ProfileSpecError(f"{path} contains a non-finite number")
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ProfileSpecError(f"{path} contains a non-string key")
                if _SENSITIVE_KEY_RE.search(key):
                    raise ProfileSpecError(
                        f"{path}.{key} looks sensitive; use an env template"
                    )
                visit(child, f"{path}.{key}")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        raise ProfileSpecError(f"{path} is not JSON-safe")

    visit(table, "kwargs")
    return json.dumps(
        table, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _concurrency(table: dict[str, Any]) -> tuple[str | None, int | None]:
    group = table.get("concurrency_group")
    limit = table.get("concurrency_limit")
    if (group is None) != (limit is None):
        raise ProfileSpecError(
            "concurrency_group and concurrency_limit must be specified together"
        )
    if group is None:
        return None, None
    return (
        _identifier(group, "concurrency_group"),
        _integer(limit, "concurrency_limit", minimum=1),
    )


def _allowed_hosts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileSpecError("extra_allowed_hosts must be an explicit array")
    result: list[str] = []
    seen: set[str] = set()
    for host in value:
        text = _string(host, "extra_allowed_hosts")
        if text != text.lower():
            raise ProfileSpecError("extra_allowed_hosts must use lowercase identities")
        try:
            ipaddress.ip_address(text)
        except ValueError:
            if _DNS_HOST_RE.fullmatch(text) is None:
                raise ProfileSpecError(
                    f"extra_allowed_hosts contains an invalid host: {text!r}"
                ) from None
        if text in seen:
            raise ProfileSpecError(
                f"extra_allowed_hosts contains a duplicate host: {text!r}"
            )
        seen.add(text)
        result.append(text)
    return tuple(result)


def _execution_identity(profile: ProfileSpec) -> tuple[Any, ...]:
    if isinstance(profile, StockProfileSpec):
        return ("stock", profile.harness)
    return (
        "custom",
        profile.import_path,
        profile.version,
        profile.models,
        profile.env,
        profile.kwargs_json,
        profile.concurrency_group,
        profile.concurrency_limit,
        profile.extra_allowed_hosts,
    )


def _expect_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileSpecError(f"{label} must be a TOML table")
    return value


def _expect_keys(
    table: dict[str, Any],
    allowed: set[str],
    label: str,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ProfileSpecError(f"{label} has unknown keys: {', '.join(unknown)}")
    missing = sorted((allowed if required is None else required) - set(table))
    if missing:
        raise ProfileSpecError(f"{label} is missing required keys: {', '.join(missing)}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileSpecError(f"{label} must be a non-empty string")
    if value != value.strip() or "\x00" in value:
        raise ProfileSpecError(f"{label} contains unsafe whitespace or NUL")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _string(value, label)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise ProfileSpecError(
            f"{label} must start with an alphanumeric and contain only "
            "letters, digits, dot, underscore, or hyphen"
        )
    return text


def _model_name(value: Any, label: str) -> str:
    text = _string(value, label)
    if any(character.isspace() for character in text):
        raise ProfileSpecError(f"{label} must not contain whitespace")
    return text


def _integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileSpecError(f"{label} must be an integer")
    if value < minimum:
        raise ProfileSpecError(f"{label} must be at least {minimum}")
    return value
