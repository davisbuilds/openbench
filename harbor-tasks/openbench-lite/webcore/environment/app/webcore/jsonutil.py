"""webcore.jsonutil -- JSON encoding aware of webcore's own types.

The stdlib :mod:`json` encoder does not know how to serialise a
:class:`~webcore.datastructures.MultiDict`, a :class:`~webcore.response.Headers`
map, or a :class:`~webcore.datastructures.CaseInsensitiveDict`, nor common value
types like :class:`datetime.datetime`, :class:`set`, or :class:`decimal.Decimal`.
:class:`WebcoreJSONEncoder` teaches the encoder those conversions in one place so
handlers can return rich objects and let the framework flatten them.

:func:`dumps` / :func:`loads` are thin wrappers that default to compact,
deterministic output (sorted keys, no whitespace) matching
:func:`webcore.response.json_response`.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
import uuid
from typing import Any, Callable, Dict, Optional

from .datastructures import CaseInsensitiveDict, MultiDict

__all__ = [
    "WebcoreJSONEncoder",
    "dumps",
    "loads",
    "to_jsonable",
    "register_default",
]

#: Registry of ``(type -> converter)`` consulted before falling back to error.
_DEFAULTS: Dict[type, Callable[[Any], Any]] = {}


def register_default(type_: type, converter: Callable[[Any], Any]) -> None:
    """Register a ``converter`` that turns instances of ``type_`` into JSON data.

    Lets an application extend serialisation without subclassing the encoder::

        register_default(MyId, lambda v: str(v))
    """
    _DEFAULTS[type_] = converter


def _default_multidict(value: MultiDict) -> Any:
    # Preserve multi-valued keys as lists; single values stay scalar.
    out: Dict[str, Any] = {}
    for key in value.keys():
        values = value.getlist(key)
        out[key] = values[0] if len(values) == 1 else values
    return out


class WebcoreJSONEncoder(json.JSONEncoder):
    """A :class:`json.JSONEncoder` that understands webcore and common types.

    Handled beyond the stdlib defaults:

    *   :class:`MultiDict` / :class:`CaseInsensitiveDict` -> ``dict``
    *   :class:`~webcore.response.Headers` -> ``dict`` of its items
    *   :class:`datetime.datetime` / ``date`` / ``time`` -> ISO 8601 string
    *   :class:`datetime.timedelta` -> total seconds (float)
    *   :class:`decimal.Decimal` -> ``float``
    *   :class:`uuid.UUID` -> ``str``
    *   :class:`set` / :class:`frozenset` -> sorted ``list``
    *   any :func:`dataclasses.dataclass` instance -> its field ``dict``
    *   anything registered via :func:`register_default`
    *   any object exposing ``to_json()`` or ``__json__()``
    """

    def default(self, o: Any) -> Any:  # noqa: D401 - see class docstring
        for type_, converter in _DEFAULTS.items():
            if isinstance(o, type_):
                return converter(o)
        if isinstance(o, MultiDict):
            return _default_multidict(o)
        if isinstance(o, CaseInsensitiveDict):
            return {key: o[key] for key in o.keys()}
        if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
            return o.isoformat()
        if isinstance(o, datetime.timedelta):
            return o.total_seconds()
        if isinstance(o, decimal.Decimal):
            return float(o)
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, (set, frozenset)):
            try:
                return sorted(o)
            except TypeError:
                return list(o)
        if isinstance(o, (bytes, bytearray)):
            return o.decode("utf-8", "replace")
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        to_json = getattr(o, "to_json", None) or getattr(o, "__json__", None)
        if callable(to_json):
            return to_json()
        # A Headers-like object exposes items() but is not a mapping json knows.
        items = getattr(o, "items", None)
        if callable(items):
            try:
                return {str(k): v for k, v in items()}
            except (TypeError, ValueError):
                pass
        return super().default(o)


def to_jsonable(value: Any) -> Any:
    """Return a plain-``json`` structure equivalent to ``value``.

    Runs ``value`` through the encoder and back so the result contains only
    ``dict``/``list``/``str``/``int``/``float``/``bool``/``None`` -- handy when a
    caller needs a serialisable snapshot rather than a string.
    """
    return json.loads(dumps(value))


def dumps(value: Any, *, sort_keys: bool = True, indent: Optional[int] = None,
          compact: bool = True) -> str:
    """Serialise ``value`` with :class:`WebcoreJSONEncoder`.

    Defaults to compact, sorted output (matching
    :func:`webcore.response.json_response`); pass ``indent`` for pretty output or
    ``compact=False`` for conventional ``", "``/``": "`` separators.
    """
    separators = (",", ":") if compact and indent is None else None
    return json.dumps(
        value,
        cls=WebcoreJSONEncoder,
        sort_keys=sort_keys,
        indent=indent,
        separators=separators,
        ensure_ascii=False,
    )


def loads(text: str) -> Any:
    """Parse a JSON string (thin wrapper over :func:`json.loads`)."""
    return json.loads(text)


def pretty(value: Any) -> str:
    """Two-space-indented, human-readable JSON for logs and debugging."""
    return dumps(value, indent=2, compact=False)
