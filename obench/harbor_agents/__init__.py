"""Optional Harbor agents.

Importing :mod:`obench` never imports Harbor. The agent class is resolved only
when Harbor (or a caller) explicitly requests it.
"""

__all__ = ["OpenBenchCodexOAuth"]


def __getattr__(name):
    if name != "OpenBenchCodexOAuth":
        raise AttributeError(name)
    from .codex import load_agent_class

    agent_class = load_agent_class()
    globals()[name] = agent_class
    return agent_class
