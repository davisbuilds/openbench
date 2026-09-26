"""Load configuration from files and strings, with ``@include`` expansion."""

import os

from miniconf.errors import ConfigError
from miniconf.parser import parse_lines

_INCLUDE = "@include"


def _expand(path, stack):
    """Return the fully include-expanded lines of the file at ``path``.

    ``stack`` is the list of absolute paths currently being expanded, used to
    detect cycles.
    """
    abspath = os.path.abspath(path)
    if abspath in stack:
        raise ConfigError("include cycle detected: {}".format(abspath))
    if not os.path.isfile(abspath):
        raise ConfigError("include target not found: {}".format(path))

    stack = stack + [abspath]
    base = os.path.dirname(abspath)
    with open(abspath, "r", encoding="utf-8") as fh:
        raw_lines = fh.read().splitlines()

    expanded = []
    for raw in raw_lines:
        parts = raw.strip().split(None, 1)
        if parts and parts[0] == _INCLUDE:
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target:
                raise ConfigError("@include requires a path")
            expanded.extend(_expand(os.path.join(base, target), stack))
        else:
            expanded.append(raw)
    return expanded


def load(path):
    """Read and parse the configuration file at ``path``, expanding includes."""
    return parse_lines(_expand(path, []))


def loads(text):
    """Parse configuration from a string."""
    return parse_lines(text.splitlines())
