"""Load optional ``.openbench/openbench.toml`` defaults for private evals.

Walks from the current working directory (or ``start``) upward looking for
``.openbench/openbench.toml``. Relative paths in the file are resolved against
the project root (the directory that contains ``.openbench/``). Explicit CLI
flags always override these defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


CONFIG_DIRNAME = ".openbench"
CONFIG_FILENAME = "openbench.toml"


@dataclass
class OpenBenchConfig:
    """Optional defaults from ``openbench.toml``."""

    path: str | None = None
    project_root: str | None = None
    tasks_dir: str | None = None
    results_path: str | None = None
    harnesses: list[str] = field(default_factory=list)
    model: str | None = None
    trials: int | None = None


def find_config_path(start: str | None = None) -> str | None:
    """Return the nearest ``.openbench/openbench.toml``, or ``None``."""
    cur = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(cur, CONFIG_DIRNAME, CONFIG_FILENAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def project_root_for_config(config_path: str) -> str:
    """Directory that contains ``.openbench/`` for ``config_path``."""
    return os.path.dirname(os.path.dirname(os.path.abspath(config_path)))


def _resolve_rel(value: str, project_root: str) -> str:
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(project_root, value))


def load_config(start: str | None = None) -> OpenBenchConfig:
    """Load config from the nearest ancestor, or an empty config if none."""
    path = find_config_path(start)
    if path is None:
        return OpenBenchConfig()
    project_root = project_root_for_config(path)
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    cfg = OpenBenchConfig(path=path, project_root=project_root)

    tasks_dir = raw.get("tasks_dir")
    if isinstance(tasks_dir, str) and tasks_dir.strip():
        cfg.tasks_dir = _resolve_rel(tasks_dir.strip(), project_root)

    results_path = raw.get("results_path")
    if isinstance(results_path, str) and results_path.strip():
        cfg.results_path = _resolve_rel(results_path.strip(), project_root)

    harnesses = raw.get("harnesses", raw.get("default_harnesses"))
    if isinstance(harnesses, str):
        cfg.harnesses = [h.strip() for h in harnesses.split(",") if h.strip()]
    elif isinstance(harnesses, list):
        cfg.harnesses = [str(h).strip() for h in harnesses if str(h).strip()]

    model = raw.get("model", raw.get("default_model"))
    if isinstance(model, str) and model.strip():
        cfg.model = model.strip()

    trials = raw.get("trials", raw.get("default_trials"))
    if isinstance(trials, bool):
        pass
    elif isinstance(trials, int) and trials >= 1:
        cfg.trials = trials
    elif isinstance(trials, float) and trials >= 1 and trials == int(trials):
        cfg.trials = int(trials)

    return cfg
