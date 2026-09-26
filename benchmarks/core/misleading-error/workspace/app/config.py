"""Application settings, loaded once at import time."""

import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS_PATH = os.path.join(_ROOT, "settings.json")

with open(_SETTINGS_PATH, "r", encoding="utf-8") as _fh:
    SETTINGS = json.load(_fh)
