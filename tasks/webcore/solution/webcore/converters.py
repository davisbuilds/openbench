"""webcore.converters -- path parameter converters.

A *converter* knows three things about a dynamic slice of a URL path:

*   ``regex`` -- the (unanchored) regular expression that a raw path segment
    must satisfy for the converter to accept it. The router splices this into
    the compiled route pattern, so the regex is also what decides whether a
    route matches a request at all.
*   ``to_python(raw)`` -- how to turn the matched *string* into the Python
    value the handler receives. ``int`` converters return an ``int`` here, the
    textual converters return the string unchanged.
*   ``to_url(value)`` -- the inverse, used by :func:`webcore.app.App.url_for`
    to render a value back into a path segment. It validates the value and
    raises :class:`ValueError` if the value could never have been produced by
    ``to_python`` (e.g. a negative number for ``int``, a string with a slash
    for ``str``).

Four converters ship with webcore:

======  ===========================================  =====================
name    matches                                      python type
======  ===========================================  =====================
str     one non-empty segment, no ``/``              ``str``
int     one or more ASCII digits                     ``int``
slug    ``[a-z0-9]+(?:-[a-z0-9]+)*``                 ``str``
path    one or more segments, ``/`` allowed          ``str`` (may hold ``/``)
======  ===========================================  =====================

``str`` is the default: a bare ``{name}`` in a route pattern uses it. ``path``
is *multi-segment* (``multi = True``) -- it swallows the remainder of the URL,
so a route may only use it in its final segment. The router enforces that.

The converters are deliberately strict. ``int`` matches digits only, which is
what makes clause-2 "fall-through" work: ``/{id:int}`` simply does not match
``/abc`` because ``abc`` never satisfies ``[0-9]+``, so the router moves on to
the next candidate route instead of raising.
"""

import re


class ConverterError(ValueError):
    """Raised when a value cannot be rendered by :meth:`Converter.to_url`."""


class Converter:
    """Base class for path converters.

    Subclasses set :attr:`name`, :attr:`regex`, and (for multi-segment
    converters) :attr:`multi`, and override :meth:`to_python` / :meth:`to_url`.
    The base implementation behaves like the ``str`` converter, which keeps the
    class usable on its own as a permissive default.
    """

    #: Public identifier used inside ``{name:converter}`` patterns.
    name = "converter"
    #: Unanchored regex a single path segment must match.
    regex = r"[^/]+"
    #: Whether this converter consumes more than one path segment.
    multi = False

    def to_python(self, raw):
        """Convert a matched path string into a Python value."""
        return raw

    def to_url(self, value):
        """Render a Python value back into a path segment string.

        Raises :class:`ConverterError` if the value is not representable.
        """
        text = value if isinstance(value, str) else str(value)
        if not re.fullmatch(self.regex, text):
            raise ConverterError(
                "value {!r} is not valid for converter {!r}".format(value, self.name)
            )
        return text

    def __repr__(self):
        return "<Converter {!r}>".format(self.name)

    def __eq__(self, other):
        return isinstance(other, Converter) and other.name == self.name

    def __hash__(self):
        return hash(self.name)


class StringConverter(Converter):
    """Default converter: exactly one non-empty segment with no slash."""

    name = "str"
    regex = r"[^/]+"

    def to_python(self, raw):
        return raw

    def to_url(self, value):
        text = value if isinstance(value, str) else str(value)
        if text == "":
            raise ConverterError("str parameter must not be empty")
        if "/" in text:
            raise ConverterError(
                "str parameter {!r} must not contain '/'".format(text)
            )
        return text


class IntConverter(Converter):
    """Non-negative integers written as ASCII digits.

    ``to_python`` returns a real Python ``int`` -- this is what clause 4
    guarantees to handlers. ``to_url`` accepts an ``int`` (or an all-digit
    string) and rejects negatives, booleans, and non-numeric input.
    """

    name = "int"
    regex = r"[0-9]+"

    def to_python(self, raw):
        return int(raw)

    def to_url(self, value):
        # bool is a subclass of int; reject it explicitly so True/False never
        # sneak through as 1/0.
        if isinstance(value, bool):
            raise ConverterError("int parameter must not be a bool")
        if isinstance(value, int):
            if value < 0:
                raise ConverterError("int parameter must be non-negative")
            return str(value)
        text = str(value)
        if not text.isdigit():
            raise ConverterError(
                "int parameter {!r} is not a non-negative integer".format(value)
            )
        return text


class SlugConverter(Converter):
    """Lowercase, digit, and hyphen slugs: ``[a-z0-9]+(?:-[a-z0-9]+)*``.

    Hyphens must join non-empty alphanumeric runs, so ``a-b-c`` is valid while
    ``-a``, ``a-``, and ``a--b`` are not.
    """

    name = "slug"
    regex = r"[a-z0-9]+(?:-[a-z0-9]+)*"

    def to_python(self, raw):
        return raw

    def to_url(self, value):
        text = value if isinstance(value, str) else str(value)
        if not re.fullmatch(self.regex, text):
            raise ConverterError(
                "slug parameter {!r} must match {}".format(text, self.regex)
            )
        return text


class PathConverter(Converter):
    """Greedy, multi-segment converter: one or more segments including ``/``.

    Because it can absorb slashes it must be the last thing in a pattern; the
    router raises if a ``path`` converter appears anywhere but the final
    segment. The matched value keeps its internal slashes but never has a
    leading or trailing one for a well-formed URL.
    """

    name = "path"
    regex = r".+"
    multi = True

    def to_python(self, raw):
        return raw

    def to_url(self, value):
        text = value if isinstance(value, str) else str(value)
        # Normalise away a leading slash so url_for joins cleanly; the value
        # must still contain at least one real character.
        text = text.lstrip("/")
        if text == "":
            raise ConverterError("path parameter must not be empty")
        return text


#: Registry of the built-in converters, keyed by their public name.
_REGISTRY = {
    StringConverter.name: StringConverter(),
    IntConverter.name: IntConverter(),
    SlugConverter.name: SlugConverter(),
    PathConverter.name: PathConverter(),
}

#: Name used when a pattern writes a bare ``{param}`` with no converter.
DEFAULT_CONVERTER = "str"


def get_converter(name):
    """Return the converter instance registered under ``name``.

    ``None`` or an empty string resolves to the default (``str``) converter.
    An unknown name raises :class:`KeyError` with a helpful message.
    """
    if name is None or name == "":
        name = DEFAULT_CONVERTER
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(
            "unknown path converter {!r} (known: {})".format(name, known)
        )


def register_converter(converter):
    """Register a custom :class:`Converter` instance (extension point)."""
    if not isinstance(converter, Converter):
        raise TypeError("converter must be a Converter instance")
    _REGISTRY[converter.name] = converter
    return converter


def converter_names():
    """Return the sorted list of registered converter names."""
    return sorted(_REGISTRY)
