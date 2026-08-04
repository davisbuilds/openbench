"""Searching and sorting over book collections."""

from catalog.text import normalize


def search_by_author(books, author):
    target = normalize(author)
    return [b for b in books if normalize(b.author) == target]


def sort_by_year(books):
    return sorted(books, key=lambda b: b.year)
