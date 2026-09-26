"""Coerce raw configuration strings into Python values."""

_BOOLS = {"true": True, "false": False}


def coerce(raw):
    """Convert a raw value string into a bool, int, float, or str.

    Booleans (case-insensitive ``true``/``false``) win, then integers, then
    floats. A value wrapped in matching single or double quotes is returned as
    the unquoted string. Anything else is returned as the trimmed string.
    """
    text = raw.strip()
    low = text.lower()
    if low in _BOOLS:
        return _BOOLS[low]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text
