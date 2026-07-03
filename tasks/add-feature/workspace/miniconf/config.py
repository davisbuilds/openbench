"""Parsed configuration container."""

from miniconf.errors import ConfigError

_MISSING = object()


class Config:
    """A parsed configuration: sections mapping keys to coerced values.

    The global section (keys before any ``[section]`` header) is stored under
    the section name ``None``.
    """

    def __init__(self):
        self._sections = {}

    def ensure_section(self, section):
        self._sections.setdefault(section, {})

    def set(self, section, key, value):
        self._sections.setdefault(section, {})[key] = value

    def get(self, key, section=None, default=_MISSING):
        """Return the value for ``key`` in ``section`` (global by default).

        Raises :class:`ConfigError` if the key is absent and no ``default`` was
        given.
        """
        values = self._sections.get(section, {})
        if key in values:
            return values[key]
        if default is _MISSING:
            raise ConfigError("no such key: [{}] {}".format(section, key))
        return default

    def sections(self):
        """Names of the non-global sections, in first-seen order."""
        return [s for s in self._sections if s is not None]

    def as_dict(self):
        return {s: dict(v) for s, v in self._sections.items()}
