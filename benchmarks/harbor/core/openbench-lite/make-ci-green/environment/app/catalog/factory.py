"""Sample catalog data used across the app."""

from catalog.books import Book


def sample_books():
    """A small fixed set of books for demos and tests.

    The demo catalog is expected to contain three titles: ``Dune``, ``1984``,
    and ``Emma``.
    """
    return [
        Book("Dune", "Herbert", 1965, 3),
        Book("1984", "Orwell", 1949, 2),
    ]
