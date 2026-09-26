"""Small text helpers."""


def normalize(value):
    """Normalize a string for comparison: trimmed and lower-cased."""
    return value.strip().lower()
