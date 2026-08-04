"""miniconf: a small INI-style configuration parser."""

from miniconf.config import Config
from miniconf.errors import ConfigError
from miniconf.loader import load, loads

__all__ = ["Config", "ConfigError", "load", "loads"]
