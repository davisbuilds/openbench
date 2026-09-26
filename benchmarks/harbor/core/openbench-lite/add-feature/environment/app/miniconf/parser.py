"""Parse configuration lines into a :class:`~miniconf.config.Config`."""

from miniconf.config import Config
from miniconf.errors import ConfigError
from miniconf.values import coerce


def parse_lines(lines):
    """Parse an iterable of raw text lines into a Config.

    Blank lines and ``#`` comments are ignored. ``[name]`` starts a section.
    ``key = value`` sets a key in the current section; a later assignment to the
    same key overrides an earlier one. Lines beginning with ``@`` are reserved
    directives handled before parsing and are ignored here. Any other line is a
    :class:`ConfigError`.
    """
    config = Config()
    section = None
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                raise ConfigError("line {}: empty section name".format(lineno))
            config.ensure_section(section)
            continue
        if line.startswith("@"):
            # Reserved directive; expanded before parsing, ignored otherwise.
            continue
        if "=" not in line:
            raise ConfigError("line {}: expected 'key = value'".format(lineno))
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError("line {}: missing key".format(lineno))
        config.set(section, key, coerce(value))
    return config
