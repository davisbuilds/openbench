"""Harbor OpenCode OAuth profile with isolated XDG credential state."""

from __future__ import annotations

import copy
import json
from pathlib import PurePosixPath
import shlex

from obench.harbor_agents._oauth import (
    capture_auth_json,
    refresh_staged_auth,
    resolve_auth_paths,
    upload_auth_json,
)
from obench.harbor_oauth import (
    HarborOAuthCaptureError,
    HarborOAuthSetupError,
    HarborOAuthUnsupportedError,
)


OPENCODE_AUTH_JSON_PATH = "OPENBENCH_OPENCODE_AUTH_JSON_PATH"
OPENCODE_AUTH_RETURN_PATH = "OPENBENCH_OPENCODE_AUTH_RETURN_PATH"

_REMOTE_HOME = PurePosixPath("/tmp/openbench-opencode-home")
_REMOTE_CONFIG_HOME = _REMOTE_HOME / ".config"
_REMOTE_DATA_HOME = _REMOTE_HOME / ".local" / "share"
_REMOTE_STATE_HOME = _REMOTE_HOME / ".local" / "state"
_REMOTE_CACHE_HOME = _REMOTE_HOME / ".cache"
_REMOTE_AUTH = _REMOTE_DATA_HOME / "opencode" / "auth.json"
_REMOTE_CONFIG = _REMOTE_CONFIG_HOME / "opencode" / "opencode.json"


def _load_harbor_opencode():
    try:
        from harbor.agents.installed.base import NonZeroAgentExitCodeError
        from harbor.agents.installed.opencode import OpenCode
    except (ImportError, ModuleNotFoundError) as exc:
        raise HarborOAuthSetupError(
            "Harbor OpenCode OAuth support requires Harbor 0.20.0"
        ) from exc
    return OpenCode, NonZeroAgentExitCodeError


def _build_agent_class(harbor_opencode, nonzero_error=RuntimeError):
    required = (
        "_deep_merge",
        "_error_messages",
        "_get_env",
        "build_cli_flags",
        "exec_as_agent",
        "exec_as_root",
        "render_instruction",
    )
    missing = [name for name in required if not hasattr(harbor_opencode, name)]
    if missing:
        raise HarborOAuthUnsupportedError(
            "installed Harbor OpenCode lacks required profile members: "
            + ", ".join(missing)
        )

    class OpenBenchOpenCodeOAuth(harbor_opencode):
        SUPPORTS_ATIF = True
        SUPPORTS_RESUME = False

        def __init__(self, *args, **kwargs):
            self._oauth_run_active = False
            super().__init__(*args, **kwargs)

        def _openbench_config(self) -> dict:
            config: dict = {}
            if self.mcp_servers:
                mcp = {}
                for server in self.mcp_servers:
                    if server.transport == "stdio":
                        command = (
                            [server.command] + server.args
                            if server.command
                            else []
                        )
                        mcp[server.name] = {
                            "type": "local",
                            "command": command,
                        }
                    else:
                        mcp[server.name] = {
                            "type": "remote",
                            "url": server.url,
                        }
                config["mcp"] = mcp
            config = self._deep_merge(
                copy.deepcopy(self._DEFAULT_CONFIG), config
            )
            return self._deep_merge(config, copy.deepcopy(self._opencode_config))

        async def run(self, instruction, environment, context):
            del context
            if self._oauth_run_active:
                raise HarborOAuthSetupError(
                    "one OpenBenchOpenCodeOAuth instance cannot run concurrently"
                )
            if self._resume:
                raise HarborOAuthUnsupportedError(
                    "OpenBenchOpenCodeOAuth does not support session resume"
                )
            input_path, return_path = resolve_auth_paths(
                self,
                input_env=OPENCODE_AUTH_JSON_PATH,
                return_env=OPENCODE_AUTH_RETURN_PATH,
            )
            if not self.model_name or "/" not in self.model_name:
                raise HarborOAuthUnsupportedError(
                    "OpenCode OAuth model must be openai/<model>"
                )
            provider, model_id = self.model_name.split("/", 1)
            if provider != "openai" or not model_id:
                raise HarborOAuthUnsupportedError(
                    "OpenBenchOpenCodeOAuth supports only the openai provider"
                )

            instruction = self.render_instruction(instruction)
            self._instruction = instruction
            remote_home = _REMOTE_HOME.as_posix()
            remote_auth = _REMOTE_AUTH.as_posix()
            staged = False
            run_error = None
            capture_error = None
            self._oauth_run_active = True
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"rm -rf {shlex.quote(remote_home)} && "
                        f"mkdir -p "
                        f"{shlex.quote(_REMOTE_AUTH.parent.as_posix())} "
                        f"{shlex.quote(_REMOTE_CONFIG.parent.as_posix())} "
                        f"{shlex.quote(_REMOTE_STATE_HOME.as_posix())} "
                        f"{shlex.quote(_REMOTE_CACHE_HOME.as_posix())}"
                    ),
                )
                await upload_auth_json(
                    self,
                    environment,
                    input_path=input_path,
                    remote_path=remote_auth,
                )
                staged = True

                config = self._openbench_config()
                if config:
                    rendered = json.dumps(config, sort_keys=True)
                    await self.exec_as_agent(
                        environment,
                        command=(
                            f"printf %s {shlex.quote(rendered)} > "
                            f"{shlex.quote(_REMOTE_CONFIG.as_posix())}"
                        ),
                    )

                if self.skills_dir:
                    await self.exec_as_agent(
                        environment,
                        command=(
                            f"mkdir -p "
                            f"{shlex.quote((_REMOTE_CONFIG_HOME / 'opencode' / 'skills').as_posix())} "
                            f"&& cp -r {shlex.quote(self.skills_dir)}/* "
                            f"{shlex.quote((_REMOTE_CONFIG_HOME / 'opencode' / 'skills').as_posix())}/ "
                            "2>/dev/null || true"
                        ),
                    )

                cli_flags = self.build_cli_flags()
                flags = f"{cli_flags} " if cli_flags else ""
                await self.exec_as_agent(
                    environment,
                    command=(
                        'OPENBENCH_REAL_HOME="$HOME"; '
                        'if [ -s "$OPENBENCH_REAL_HOME/.nvm/nvm.sh" ]; then '
                        '. "$OPENBENCH_REAL_HOME/.nvm/nvm.sh"; fi; '
                        f"export HOME={shlex.quote(remote_home)} "
                        f"XDG_CONFIG_HOME={shlex.quote(_REMOTE_CONFIG_HOME.as_posix())} "
                        f"XDG_DATA_HOME={shlex.quote(_REMOTE_DATA_HOME.as_posix())} "
                        f"XDG_STATE_HOME={shlex.quote(_REMOTE_STATE_HOME.as_posix())} "
                        f"XDG_CACHE_HOME={shlex.quote(_REMOTE_CACHE_HOME.as_posix())} "
                        "OPENCODE_FAKE_VCS=git; "
                        "unset OPENAI_API_KEY OPENAI_BASE_URL OPENCODE_CONFIG "
                        "OPENCODE_CONFIG_DIR OPENCODE_CONFIG_CONTENT; "
                        f"opencode --model=openai/{shlex.quote(model_id)} "
                        "run --format=json "
                        f"{flags}"
                        "--thinking --dangerously-skip-permissions -- "
                        f"{shlex.quote(instruction)} "
                        "2>&1 </dev/null | "
                        "stdbuf -oL tee /logs/agent/opencode.txt"
                    ),
                )
                if messages := self._error_messages():
                    raise nonzero_error(
                        "OpenCode emitted error event(s): "
                        + "; ".join(messages[:3])
                    )
            except BaseException as exc:
                run_error = exc
            finally:
                if staged:
                    try:
                        await capture_auth_json(
                            environment,
                            remote_path=remote_auth,
                            return_path=return_path,
                            harness="OpenCode",
                        )
                        refresh_staged_auth(input_path, return_path)
                    except HarborOAuthCaptureError as exc:
                        capture_error = exc
                try:
                    await self.exec_as_agent(
                        environment,
                        command=f"rm -rf {shlex.quote(remote_home)}",
                    )
                except BaseException:
                    pass
                self._oauth_run_active = False

            if capture_error is not None:
                raise capture_error from run_error
            if run_error is not None:
                raise run_error

    OpenBenchOpenCodeOAuth.__name__ = "OpenBenchOpenCodeOAuth"
    OpenBenchOpenCodeOAuth.__qualname__ = "OpenBenchOpenCodeOAuth"
    OpenBenchOpenCodeOAuth.__module__ = __name__
    return OpenBenchOpenCodeOAuth


def load_agent_class():
    base, nonzero_error = _load_harbor_opencode()
    return _build_agent_class(base, nonzero_error)


def __getattr__(name):
    if name != "OpenBenchOpenCodeOAuth":
        raise AttributeError(name)
    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
