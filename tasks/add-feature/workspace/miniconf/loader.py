"""Load configuration from files and strings."""

from miniconf.parser import parse_lines


def load(path):
    """Read and parse the configuration file at ``path``."""
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    return parse_lines(lines)


def loads(text):
    """Parse configuration from a string."""
    return parse_lines(text.splitlines())
