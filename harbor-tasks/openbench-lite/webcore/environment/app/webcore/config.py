"""webcore.config -- a layered application configuration object.

:class:`Config` is a dict-like store for application settings with a few
conveniences the raw ``dict`` lacks: loading from a mapping or from environment
variables under a prefix, typed getters that coerce and validate, dotted-key
namespaces, and an immutable snapshot for handing config to request handlers
without risking mutation.

It stays deliberately small -- no file formats, no import-a-module magic -- so it
has zero dependencies beyond the stdlib.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Iterator, List, Mapping, MutableMapping, Optional

__all__ = ["Config", "ConfigError"]


class ConfigError(Exception):
    """Raised for a missing required key or a failed type coercion."""


#: Strings accepted as boolean true / false by :meth:`Config.get_bool`.
_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f"})


class Config(MutableMapping):
    """A mutable, dict-like configuration mapping with typed accessors."""

    def __init__(self, initial: Optional[Mapping[str, Any]] = None,
                 defaults: Optional[Mapping[str, Any]] = None) -> None:
        self._data: Dict[str, Any] = {}
        if defaults:
            self._data.update(defaults)
        if initial:
            self._data.update(initial)

    # -- MutableMapping protocol ----------------------------------------

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # -- loading ---------------------------------------------------------

    def update_from(self, mapping: Mapping[str, Any]) -> "Config":
        """Merge ``mapping`` into this config (last write wins). Returns self."""
        self._data.update(mapping)
        return self

    def load_env(self, prefix: str, lowercase: bool = True) -> "Config":
        """Load environment variables whose names start with ``prefix``.

        ``prefix="APP_"`` picks up ``APP_DEBUG`` as key ``debug`` (or ``DEBUG``
        when ``lowercase=False``). Values remain strings; use the typed getters
        to coerce them.
        """
        for name, value in os.environ.items():
            if not name.startswith(prefix):
                continue
            key = name[len(prefix):]
            key = key.lower() if lowercase else key
            self._data[key] = value
        return self

    def namespace(self, prefix: str, strip: bool = True) -> "Config":
        """Return a sub-:class:`Config` of keys under a dotted ``prefix``.

        ``config.namespace("db")`` gathers ``db.host``/``db.port`` into a new
        config keyed ``host``/``port`` (with ``strip=True``).
        """
        dotted = prefix if prefix.endswith(".") else prefix + "."
        out: Dict[str, Any] = {}
        for key, value in self._data.items():
            if key.startswith(dotted):
                out[key[len(dotted):] if strip else key] = value
        return Config(out)

    # -- typed access ----------------------------------------------------

    def get_str(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = self._data.get(key, default)
        return None if value is None else str(value)

    def get_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        return self._coerce(key, int, default)

    def get_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        return self._coerce(key, float, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Coerce a config value to ``bool`` using a permissive string table."""
        if key not in self._data:
            return default
        value = self._data[key]
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
        raise ConfigError("cannot read {!r} as bool: {!r}".format(key, value))

    def get_list(self, key: str, separator: str = ",",
                 default: Optional[List[str]] = None) -> List[str]:
        """Read a delimited string (or an existing list) as a list of strings."""
        if key not in self._data:
            return list(default) if default is not None else []
        value = self._data[key]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return [part.strip() for part in str(value).split(separator) if part.strip()]

    def require(self, key: str) -> Any:
        """Return ``key`` or raise :class:`ConfigError` if it is absent."""
        if key not in self._data:
            raise ConfigError("missing required config key {!r}".format(key))
        return self._data[key]

    def _coerce(self, key: str, caster: Callable[[Any], Any], default: Any) -> Any:
        if key not in self._data:
            return default
        try:
            return caster(self._data[key])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "cannot read {!r} as {}: {!r}".format(
                    key, caster.__name__, self._data[key])) from exc

    # -- snapshots -------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a shallow ``dict`` copy safe to hand out to handlers."""
        return dict(self._data)

    def copy(self) -> "Config":
        """Return an independent :class:`Config` with the same data."""
        return Config(self._data)

    def __repr__(self) -> str:
        return "<Config {} keys>".format(len(self._data))
