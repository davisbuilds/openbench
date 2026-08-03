"""Sequential-trial profile layered on the existing Harbor Codex OAuth agent."""

from __future__ import annotations

from pathlib import Path

from obench.harbor_agents._oauth import refresh_staged_auth
from obench.harbor_agents.codex import load_agent_class as load_codex_oauth_class
from obench.harbor_oauth import (
    CODEX_AUTH_JSON_PATH,
    CODEX_AUTH_RETURN_PATH,
    HarborOAuthSetupError,
)


def _build_agent_class(codex_oauth):
    class OpenBenchCodexOAuthProfile(codex_oauth):
        """Refresh the shared staged input after each captured rotation."""

        async def run(self, instruction, environment, context):
            input_value = self._get_env(CODEX_AUTH_JSON_PATH)
            return_value = self._get_env(CODEX_AUTH_RETURN_PATH)
            if not input_value or not return_value:
                raise HarborOAuthSetupError(
                    "Codex OAuth input and return paths are required"
                )
            input_path = Path(input_value)
            return_path = Path(return_value)
            try:
                return await super().run(instruction, environment, context)
            finally:
                if return_path.is_file():
                    refresh_staged_auth(input_path, return_path)

    OpenBenchCodexOAuthProfile.__name__ = "OpenBenchCodexOAuthProfile"
    OpenBenchCodexOAuthProfile.__qualname__ = "OpenBenchCodexOAuthProfile"
    OpenBenchCodexOAuthProfile.__module__ = __name__
    return OpenBenchCodexOAuthProfile


def load_agent_class():
    return _build_agent_class(load_codex_oauth_class())


def __getattr__(name):
    if name != "OpenBenchCodexOAuthProfile":
        raise AttributeError(name)
    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
