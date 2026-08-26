"""Load operator-defined open-model routes from a TOML file.

Adapters ship a built-in ``OPEN_MODELS`` registry; this lets an operator add
OpenRouter / BYO routes without editing adapter code. Entries are read from
``$OPENBENCH_OPEN_MODELS`` (else ``~/.openbench/open_models.toml``) and MERGED
over the built-ins, so a config entry can add a new model or override a built-in
of the same name. Malformed entries are skipped -- a bad config never crashes an
adapter, which would otherwise take down an entire benchmark run.

Config schema (TOML)::

    [models."glm-5.3-flash"]
    provider = "openrouter"
    model_id = "glm-5.3-flash"
    env_key  = "OPENROUTER_API_KEY"
    display  = "Z.ai GLM-5.3 Flash"
    effort   = "medium"
    base_url = "https://openrouter.ai/api/v1"   # optional (informational)

Kept stdlib-only (``tomllib``, 3.11+) so it stays importable under the runner's
isolated adapter importer, alongside ~/.openbench/keys.env.
"""

import os

DEFAULT_CONFIG_PATH = "~/.openbench/open_models.toml"
_REQUIRED_KEYS = ("provider", "model_id", "env_key", "display", "effort")


def config_path(path=None):
    """Resolve the config path: explicit arg, else env, else the default."""
    if path is not None:
        return os.path.expanduser(path)
    return os.path.expanduser(
        os.environ.get("OPENBENCH_OPEN_MODELS", DEFAULT_CONFIG_PATH))


def load_open_model_overrides(path=None):
    """Return ``{name: spec}`` from the config file, or ``{}`` if absent/unreadable."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11: no stdlib TOML reader
        return {}
    cfg = config_path(path)
    if not os.path.isfile(cfg):
        return {}
    try:
        with open(cfg, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        # Unreadable or malformed TOML must not crash the importing adapter.
        return {}
    raw = data.get("models")
    if not isinstance(raw, dict):
        raw = data.get("open_models")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        if any(k not in spec for k in _REQUIRED_KEYS):
            continue  # incomplete entry -> skip rather than half-wire a route
        out[str(name)] = {
            "provider": spec["provider"],
            "model_id": spec["model_id"],
            "base_url": spec.get("base_url", ""),
            "env_key": spec["env_key"],
            "display": spec["display"],
            "effort": spec["effort"],
        }
    return out


def merge_open_models(builtin, path=None):
    """Built-in registry overlaid with operator config (config wins on collision)."""
    return {**builtin, **load_open_model_overrides(path)}
