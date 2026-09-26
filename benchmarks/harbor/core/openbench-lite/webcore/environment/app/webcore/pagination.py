"""webcore.pagination -- helpers for paginating list endpoints.

Turning a large collection into pages is repetitive: clamp the page number,
slice the sequence, compute the total count, and emit navigation links. This
module centralises that into :class:`Page` (a computed slice with metadata) and
:func:`paginate` (which reads ``page``/``per_page`` from a request's query and
builds the page), plus link helpers that cooperate with ``url_for``.

It works on any in-memory sequence and produces plain data structures, so a
handler can wrap the result in :func:`webcore.response.json_response` directly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = ["Page", "paginate", "page_links"]


def _clamp(value: int, low: int, high: Optional[int]) -> int:
    if value < low:
        return low
    if high is not None and value > high:
        return high
    return value


class Page:
    """One page of a larger sequence, with navigation metadata.

    Attributes
    ----------
    items:
        The slice of results for this page.
    number:
        The 1-based page number.
    per_page:
        Items per page requested.
    total:
        Total number of items across all pages.
    """

    def __init__(self, items: List[Any], number: int, per_page: int, total: int) -> None:
        self.items = items
        self.number = number
        self.per_page = per_page
        self.total = total

    @property
    def pages(self) -> int:
        """Total number of pages (at least 1, even when empty)."""
        if self.per_page <= 0:
            return 1
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    @property
    def prev_number(self) -> Optional[int]:
        return self.number - 1 if self.has_prev else None

    @property
    def next_number(self) -> Optional[int]:
        return self.number + 1 if self.has_next else None

    @property
    def start_index(self) -> int:
        """1-based index of the first item on this page (0 if empty)."""
        return 0 if not self.items else (self.number - 1) * self.per_page + 1

    @property
    def end_index(self) -> int:
        """1-based index of the last item on this page (0 if empty)."""
        return 0 if not self.items else self.start_index + len(self.items) - 1

    def to_dict(self, key: str = "items") -> Dict[str, Any]:
        """A JSON-ready envelope: the items plus a ``meta`` block."""
        return {
            key: self.items,
            "meta": {
                "page": self.number,
                "per_page": self.per_page,
                "total": self.total,
                "pages": self.pages,
                "has_prev": self.has_prev,
                "has_next": self.has_next,
            },
        }

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return "<Page {}/{} ({} items)>".format(self.number, self.pages, len(self.items))


def paginate(sequence: Sequence[Any], page: int = 1, per_page: int = 20,
             max_per_page: int = 100) -> Page:
    """Slice ``sequence`` into a :class:`Page`.

    ``page`` is clamped to at least 1 and ``per_page`` to ``[1, max_per_page]``,
    so out-of-range query values degrade gracefully rather than raising.
    """
    per_page = _clamp(int(per_page), 1, max_per_page)
    page = _clamp(int(page), 1, None)
    total = len(sequence)
    start = (page - 1) * per_page
    end = start + per_page
    items = list(sequence[start:end])
    return Page(items, page, per_page, total)


def paginate_request(request, sequence: Sequence[Any], per_page: int = 20,
                     max_per_page: int = 100) -> Page:
    """Read ``page``/``per_page`` from ``request.query`` and paginate.

    Non-integer query values fall back to the defaults. Handy for a handler that
    exposes ``?page=2&per_page=50``.
    """
    def _int(name: str, default: int) -> int:
        raw = request.query.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    return paginate(sequence, _int("page", 1), _int("per_page", per_page), max_per_page)


def page_links(page: Page, url_builder: Callable[[int], str]) -> Dict[str, Optional[str]]:
    """Build a ``{first,prev,self,next,last}`` link map for ``page``.

    ``url_builder`` maps a page number to a URL (e.g. a closure over
    ``app.url_for``). Absent links (no previous on page 1) are ``None``.
    """
    return {
        "first": url_builder(1),
        "prev": url_builder(page.prev_number) if page.prev_number else None,
        "self": url_builder(page.number),
        "next": url_builder(page.next_number) if page.next_number else None,
        "last": url_builder(page.pages),
    }
