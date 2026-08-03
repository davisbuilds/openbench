"""Pinned Harbor agent profiles for OpenBench harness comparisons.

The resolver is intentionally independent of Harbor.  It returns the inputs for
Harbor's ``AgentConfig`` and leaves task selection, trials, retries, and
scheduling to Harbor and the OpenBench Harbor job integration.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


HARBOR_VERSION = "0.20.0"
AUTH_STRATEGY_OAUTH = "oauth"

CODEX_PROFILE_IMPORT = (
    "obench.harbor_agents.codex_profile:OpenBenchCodexOAuthProfile"
)
PI_PROFILE_IMPORT = "obench.harbor_agents.pi:OpenBenchPiOAuth"
OPENCODE_PROFILE_IMPORT = "obench.harbor_agents.opencode:OpenBenchOpenCodeOAuth"

_MODEL_PREFIX_BY_PROFILE = {
    CODEX_PROFILE_IMPORT: "",
    PI_PROFILE_IMPORT: "openai-codex/",
    OPENCODE_PROFILE_IMPORT: "openai/",
}

_MODEL_IDS = {
    "gpt-5.5-medium": "gpt-5.5",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna": "gpt-5.6-luna",
}


class HarborProfileError(ValueError):
    """A requested harness profile cannot be represented without ambiguity."""


@dataclass(frozen=True)
class HarborAuthContract:
    """Host-owned credential staging and persist-back requirements."""

    strategy: str
    source_candidates: tuple[str, ...]
    input_env: str
    return_env: str
    persist_back: bool = True
    lease_required: bool = True
    max_concurrent_uses: int = 1
    concurrency_group: str = ""


@dataclass(frozen=True)
class HarborProxyContract:
    """How a profile can receive an OpenBench counting-proxy cell URL."""

    supported: bool
    route: str | None
    agent_env: str | None
    configuration: str


@dataclass(frozen=True)
class HarborHarnessProfile:
    """Immutable, reproducible inputs for one Harbor agent/model arm."""

    harness: str
    model: str
    semantic_name: str
    agent_import_path: str
    cli_version: str
    harbor_model_name: str
    flags: tuple[tuple[str, str], ...]
    config_json: str | None
    auth: HarborAuthContract
    proxy: HarborProxyContract
    agent_env: tuple[tuple[str, str], ...] = ()

    def agent_kwargs(self) -> dict[str, Any]:
        """Return fresh kwargs accepted by Harbor 0.20.0 installed agents."""

        kwargs: dict[str, Any] = {"version": self.cli_version}
        kwargs.update(self.flags)
        if self.config_json is not None:
            kwargs["config"] = json.loads(self.config_json)
        return kwargs

    def agent_config(
        self,
        *,
        auth_json_path: str,
        auth_return_path: str,
    ) -> dict[str, Any]:
        """Return programmatic ``harbor.models.trial.config.AgentConfig`` data.

        Credential contents are never accepted here.  The caller must hold the
        declared host lease for both paths' complete stage/run/persist lifecycle.
        """

        if not isinstance(auth_json_path, str) or not auth_json_path.startswith("/"):
            raise HarborProfileError("auth_json_path must be an absolute path")
        if not isinstance(auth_return_path, str) or not auth_return_path.startswith("/"):
            raise HarborProfileError("auth_return_path must be an absolute path")
        if auth_json_path == auth_return_path:
            raise HarborProfileError("auth input and return paths must be distinct")

        env = dict(self.agent_env)
        env[self.auth.input_env] = auth_json_path
        env[self.auth.return_env] = auth_return_path
        return {
            "name": None,
            "import_path": self.agent_import_path,
            "model_name": self.harbor_model_name,
            "n_concurrent": self.auth.max_concurrent_uses,
            "concurrency_group": self.auth.concurrency_group,
            "kwargs": self.agent_kwargs(),
            "env": env,
        }


_AUTH = {
    "codex": HarborAuthContract(
        strategy=AUTH_STRATEGY_OAUTH,
        source_candidates=("~/.codex/auth.json",),
        input_env="CODEX_AUTH_JSON_PATH",
        return_env="OPENBENCH_CODEX_AUTH_RETURN_PATH",
        concurrency_group="openbench-oauth-codex",
    ),
    "pi": HarborAuthContract(
        strategy=AUTH_STRATEGY_OAUTH,
        source_candidates=("~/.pi/agent/auth.json",),
        input_env="OPENBENCH_PI_AUTH_JSON_PATH",
        return_env="OPENBENCH_PI_AUTH_RETURN_PATH",
        concurrency_group="openbench-oauth-pi",
    ),
    "opencode": HarborAuthContract(
        strategy=AUTH_STRATEGY_OAUTH,
        source_candidates=(
            "~/.local/share/opencode/auth.json",
            "~/.opencode/data/auth.json",
        ),
        input_env="OPENBENCH_OPENCODE_AUTH_JSON_PATH",
        return_env="OPENBENCH_OPENCODE_AUTH_RETURN_PATH",
        concurrency_group="openbench-oauth-opencode",
    ),
}

_PROXY = {
    "codex": HarborProxyContract(
        supported=True,
        route="codex/backend-api/codex",
        agent_env="OPENAI_BASE_URL",
        configuration="Harbor Codex openai_base_url",
    ),
    "pi": HarborProxyContract(
        supported=True,
        route="codex/backend-api",
        agent_env="OPENBENCH_PI_BASE_URL",
        configuration="isolated Pi models.json openai-codex.baseUrl",
    ),
    "opencode": HarborProxyContract(
        supported=False,
        route=None,
        agent_env=None,
        configuration=(
            "OpenCode OAuth base-URL routing is not source-proven in the "
            "OpenBench adapter"
        ),
    ),
}

_VERSIONS = {
    "codex": "0.144.5",
    "pi": "0.80.10",
    "opencode": "1.18.3",
}

_IMPORTS = {
    "codex": CODEX_PROFILE_IMPORT,
    "pi": PI_PROFILE_IMPORT,
    "opencode": OPENCODE_PROFILE_IMPORT,
}


def canonical_openbench_model(
    agent_import_path: str,
    harbor_model_name: str,
) -> str:
    """Return the shared comparison identity for a pinned Harbor profile."""

    prefix = _MODEL_PREFIX_BY_PROFILE.get(agent_import_path)
    if prefix is None:
        return harbor_model_name
    if prefix and not harbor_model_name.startswith(prefix):
        raise HarborProfileError(
            f"Harbor model {harbor_model_name!r} does not match the "
            f"{agent_import_path!r} profile prefix {prefix!r}"
        )
    canonical = harbor_model_name[len(prefix):]
    if not canonical:
        raise HarborProfileError("Harbor model identity has no canonical model")
    return canonical


def expected_harbor_model_name(
    agent_import_path: str,
    canonical_model: str,
) -> str:
    """Reconstruct the exact Harbor lock identity for a comparison row."""

    prefix = _MODEL_PREFIX_BY_PROFILE.get(agent_import_path)
    return canonical_model if prefix is None else f"{prefix}{canonical_model}"


def _codex_config(model: str) -> str:
    config: dict[str, Any] = {
        "features": {
            "apps": False,
            "plugins": False,
            "multi_agent": False,
        },
    }
    if model.startswith("gpt-5.6-"):
        config["service_tier"] = "default"
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def resolve_harbor_profile(
    harness: str,
    model: str,
    *,
    auth_strategy: str = AUTH_STRATEGY_OAUTH,
    proxy_base_url: str | None = None,
) -> HarborHarnessProfile:
    """Resolve one supported OpenBench arm, rejecting implicit fallbacks."""

    if harness not in _IMPORTS:
        raise HarborProfileError(
            f"unsupported Harbor harness {harness!r}; "
            f"expected one of {sorted(_IMPORTS)}"
        )
    if model not in _MODEL_IDS:
        raise HarborProfileError(
            f"unsupported {harness} Harbor model {model!r}; "
            f"expected one of {sorted(_MODEL_IDS)}"
        )
    if auth_strategy != AUTH_STRATEGY_OAUTH:
        raise HarborProfileError(
            f"unsupported {harness} auth strategy {auth_strategy!r}; "
            "OpenBench Harbor profiles require oauth"
        )
    if proxy_base_url is not None:
        if not isinstance(proxy_base_url, str) or not proxy_base_url.startswith(
            ("http://", "https://")
        ):
            raise HarborProfileError(
                "proxy_base_url must be an absolute HTTP(S) URL"
            )
        if not _PROXY[harness].supported:
            raise HarborProfileError(
                f"{harness} OAuth counting-proxy routing is unsupported"
            )

    model_id = _MODEL_IDS[model]
    if harness == "codex":
        harbor_model = model_id
        flags = (("reasoning_effort", "medium"),)
        config_json = _codex_config(model)
    elif harness == "pi":
        harbor_model = f"openai-codex/{model_id}"
        flags = (("thinking", "medium"),)
        config_json = None
    else:
        harbor_model = f"openai/{model_id}"
        flags = (("variant", "medium"),)
        config_json = None

    env: tuple[tuple[str, str], ...] = ()
    proxy = _PROXY[harness]
    if proxy_base_url is not None:
        env = ((proxy.agent_env, proxy_base_url.rstrip("/")),)  # type: ignore[arg-type]

    return HarborHarnessProfile(
        harness=harness,
        model=model,
        semantic_name=harness,
        agent_import_path=_IMPORTS[harness],
        cli_version=_VERSIONS[harness],
        harbor_model_name=harbor_model,
        flags=flags,
        config_json=config_json,
        auth=_AUTH[harness],
        proxy=proxy,
        agent_env=env,
    )


def supported_harbor_matrix() -> tuple[tuple[str, str], ...]:
    """Return the complete, stable harness/model compatibility matrix."""

    return tuple(
        (harness, model)
        for harness in sorted(_IMPORTS)
        for model in sorted(_MODEL_IDS)
    )
