"""Report computations."""

from app import pricing


def compute_total(count):
    """Total charge for ``count`` items at the configured rate."""
    rate = pricing.get_rate()
    return rate * count
