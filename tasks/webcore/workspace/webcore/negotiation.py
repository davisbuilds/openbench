"""webcore.negotiation -- HTTP content negotiation.

Proactive negotiation is the server picking the best representation for a client
from the ``Accept`` family of headers. This module parses those headers into
quality-ranked lists and answers the two questions handlers actually ask:

*   "of the representations I can produce, which does the client prefer?"
    (:meth:`Accept.best_match`)
*   "does the client accept this specific one at all?" (:meth:`Accept.quality`)

It covers media types (``Accept``), languages (``Accept-Language``), charsets
(``Accept-Charset``) and encodings (``Accept-Encoding``) through one small
:class:`Accept` value object, with media types getting the extra wildcard rules
(``text/*``, ``*/*``) their grammar requires.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Accept",
    "MediaType",
    "parse_accept",
    "parse_accept_language",
    "best_media_type",
    "best_language",
]


def _split_quality(item: str) -> Tuple[str, float, dict]:
    """Split one ``Accept`` list item into ``(value, quality, extra_params)``."""
    pieces = [p.strip() for p in item.split(";") if p.strip()]
    if not pieces:
        return "", 0.0, {}
    value = pieces[0].lower()
    quality = 1.0
    params: dict = {}
    for piece in pieces[1:]:
        if "=" not in piece:
            continue
        key, _, raw = piece.partition("=")
        key = key.strip().lower()
        raw = raw.strip().strip('"')
        if key == "q":
            try:
                quality = max(0.0, min(1.0, float(raw)))
            except ValueError:
                quality = 0.0
        else:
            params[key] = raw
    return value, quality, params


def parse_accept(header: str) -> List[Tuple[str, float]]:
    """Parse a generic ``Accept`` header into ``[(value, quality), ...]``.

    Sorted by descending quality; items with equal quality keep the header's
    original left-to-right order (a stable sort over an enumerated index).
    """
    if not header:
        return []
    scored = []
    for index, raw_item in enumerate(header.split(",")):
        raw_item = raw_item.strip()
        if not raw_item:
            continue
        value, quality, _params = _split_quality(raw_item)
        if value:
            scored.append((value, quality, index))
    scored.sort(key=lambda t: (-t[1], t[2]))
    return [(value, quality) for value, quality, _ in scored]


def parse_accept_language(header: str) -> List[Tuple[str, float]]:
    """Parse an ``Accept-Language`` header (an alias tuned name for clarity)."""
    return parse_accept(header)


class MediaType:
    """A parsed ``type/subtype`` with wildcard-aware matching.

    ``text/*`` matches any ``text/...`` offer; ``*/*`` matches anything. Equality
    of specificity (``self.matches(other)``) is what powers media-type wildcards
    in :meth:`Accept.quality`.
    """

    __slots__ = ("type", "subtype")

    def __init__(self, value: str) -> None:
        value = value.strip().lower()
        if "/" in value:
            self.type, _, self.subtype = value.partition("/")
        else:
            self.type, self.subtype = value, "*"

    def matches(self, offer: "MediaType") -> bool:
        """True if this (possibly wildcard) pattern accepts a concrete ``offer``."""
        if self.type == "*":
            return True
        if self.type != offer.type:
            return False
        return self.subtype in ("*", offer.subtype)

    def specificity(self) -> int:
        """A 0-2 rank: ``*/*`` < ``type/*`` < ``type/subtype``."""
        if self.type == "*":
            return 0
        if self.subtype == "*":
            return 1
        return 2

    def __str__(self) -> str:
        return "{}/{}".format(self.type, self.subtype)

    def __repr__(self) -> str:
        return "<MediaType {}>".format(self)


class Accept:
    """A quality-ranked accept list with best-match resolution.

    Construct from a parsed ``[(value, quality)]`` list (see :func:`parse_accept`)
    or use :meth:`from_header`. Set ``media=True`` for media-type wildcard
    semantics; leave it false for languages, charsets and encodings, which match
    by exact token (with ``*`` as the catch-all).
    """

    def __init__(self, items: Sequence[Tuple[str, float]], media: bool = False) -> None:
        self.items: List[Tuple[str, float]] = list(items)
        self.media = media

    @classmethod
    def from_header(cls, header: str, media: bool = False) -> "Accept":
        return cls(parse_accept(header), media=media)

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def quality(self, offer: str) -> float:
        """Return the client's quality weight for ``offer`` (0.0 if unacceptable).

        For a media Accept, wildcards apply and the *most specific* matching
        accept entry wins. An empty accept list means "no preference", scored as
        1.0 for every offer -- the conventional default.
        """
        if not self.items:
            return 1.0
        if self.media:
            return self._media_quality(offer)
        offer_l = offer.lower()
        best = 0.0
        for value, quality in self.items:
            if value == offer_l or value == "*":
                best = max(best, quality)
        return best

    def _media_quality(self, offer: str) -> float:
        offer_type = MediaType(offer)
        best_quality = 0.0
        best_specificity = -1
        for value, quality in self.items:
            pattern = MediaType(value)
            if pattern.matches(offer_type):
                spec = pattern.specificity()
                if spec > best_specificity or (spec == best_specificity and quality > best_quality):
                    best_specificity = spec
                    best_quality = quality
        return best_quality

    def accepts(self, offer: str) -> bool:
        """True if ``offer`` has a non-zero quality."""
        return self.quality(offer) > 0.0

    def best_match(self, offers: Iterable[str],
                   default: Optional[str] = None) -> Optional[str]:
        """Return the offer the client prefers most, or ``default``.

        Offers are the representations *the server* can produce. Each is scored by
        the client's accept list; the highest-scoring offer wins, ties broken by
        the server's own order (earlier offer wins). An offer scoring 0 is never
        returned.
        """
        best_offer = default
        best_quality = 0.0
        for offer in offers:
            quality = self.quality(offer)
            if quality > best_quality:
                best_quality = quality
                best_offer = offer
        return best_offer

    def __repr__(self) -> str:
        return "<Accept {!r}>".format(self.items)


def best_media_type(header: str, offers: Sequence[str],
                    default: Optional[str] = None) -> Optional[str]:
    """One-shot: parse a media ``Accept`` header and pick the best of ``offers``."""
    return Accept.from_header(header, media=True).best_match(offers, default)


def best_language(header: str, offers: Sequence[str],
                  default: Optional[str] = None) -> Optional[str]:
    """One-shot: pick the client's preferred language from ``offers``."""
    return Accept.from_header(header, media=False).best_match(offers, default)
