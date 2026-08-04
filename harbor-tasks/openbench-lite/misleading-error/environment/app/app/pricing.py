"""Pricing helpers derived from application settings."""

from app.config import SETTINGS


def get_rate():
    """The per-item rate configured for the service."""
    return SETTINGS.get("rate")
