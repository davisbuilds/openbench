"""webcore.converters -- path parameter handling.

Today webcore has a single kind of path parameter: a ``{name}`` placeholder
that captures exactly one non-empty path segment (no ``/``) and hands it to the
handler as a string. The :class:`StringConverter` below encapsulates that one
behaviour; it exists as its own class so the framework has a clean place to grow
a richer set of typed converters later.
"""

import re


class Converter:
    """Base class for a path converter."""

    name = "str"
    regex = r"[^/]+"

    def to_python(self, raw):
        return raw

    def to_url(self, value):
        return value if isinstance(value, str) else str(value)

    def __repr__(self):
        return "<Converter {!r}>".format(self.name)


class StringConverter(Converter):
    """One non-empty path segment, delivered to the handler as a string."""

    name = "str"
    regex = r"[^/]+"


#: The only converter webcore currently understands.
_STRING = StringConverter()


def get_converter(name=None):
    """Return the string converter (the only converter available today)."""
    return _STRING
