"""webcore.datastructures -- small container types used across webcore.

*   :class:`MultiDict` -- a mapping where one key may hold several values, with
    dict-like access returning the *first* value and explicit ``getlist`` /
    ``getall`` for the rest. Query strings (``?tag=a&tag=b``) and form bodies
    are naturally multi-valued, so this is what backs
    :attr:`webcore.request.Request.query_multi`.
*   :class:`ImmutableMultiDict` -- a read-only :class:`MultiDict` for values that
    should not be mutated after a request is constructed.
*   :class:`CaseInsensitiveDict` -- a mapping with case-insensitive string keys
    (used for header-like data that is not the response's own ``Headers``).

These are deliberately minimal -- just enough surface to be ergonomic, not a
full ``collections.abc`` implementation.
"""


_MISSING = object()


class MultiDict:
    """A mapping that allows multiple values per key, preserving insertion order."""

    def __init__(self, mapping=None):
        # Ordered dict of key -> list of values.
        self._data = {}
        if mapping is None:
            pairs = []
        elif isinstance(mapping, MultiDict):
            pairs = mapping.items(multi=True)
        elif hasattr(mapping, "items"):
            pairs = list(mapping.items())
        else:
            pairs = list(mapping)
        for key, value in pairs:
            self.add(key, value)

    def add(self, key, value):
        """Append ``value`` to the list stored under ``key``."""
        self._data.setdefault(key, []).append(value)

    def __setitem__(self, key, value):
        """Replace any existing values for ``key`` with a single ``value``."""
        self._data[key] = [value]

    def __getitem__(self, key):
        values = self._data.get(key)
        if not values:
            raise KeyError(key)
        return values[0]

    def __delitem__(self, key):
        del self._data[key]

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def get(self, key, default=None):
        values = self._data.get(key)
        return values[0] if values else default

    def getlist(self, key):
        """Return the list of values for ``key`` (empty list if absent)."""
        return list(self._data.get(key, []))

    def getall(self, key):
        """Alias for :meth:`getlist`."""
        return self.getlist(key)

    def setdefault(self, key, default=None):
        if key not in self._data:
            self._data[key] = [default]
        return self._data[key][0]

    def keys(self):
        return list(self._data.keys())

    def values(self):
        """First value for each key."""
        return [values[0] for values in self._data.values()]

    def items(self, multi=False):
        """Return ``(key, value)`` pairs.

        With ``multi=True`` every value is yielded (so a key with N values
        appears N times); otherwise only the first value per key is returned.
        """
        result = []
        for key, values in self._data.items():
            if multi:
                for value in values:
                    result.append((key, value))
            else:
                result.append((key, values[0]))
        return result

    def to_dict(self, flat=True):
        """Return a plain ``dict``.

        ``flat=True`` keeps only the first value per key; ``flat=False`` maps
        each key to its full list.
        """
        if flat:
            return {key: values[0] for key, values in self._data.items()}
        return {key: list(values) for key, values in self._data.items()}

    def copy(self):
        new = MultiDict()
        new._data = {key: list(values) for key, values in self._data.items()}
        return new

    def __eq__(self, other):
        if isinstance(other, MultiDict):
            return self._data == other._data
        if isinstance(other, dict):
            return self.to_dict(flat=True) == other
        return NotImplemented

    def __repr__(self):
        return "MultiDict({!r})".format(self.items(multi=True))


class ImmutableMultiDict(MultiDict):
    """A :class:`MultiDict` that rejects mutation after construction."""

    def _frozen(self, *args, **kwargs):
        raise TypeError("ImmutableMultiDict is read-only")

    add = _frozen
    __setitem__ = _frozen
    __delitem__ = _frozen
    setdefault = _frozen

    def copy(self):
        """Return a *mutable* :class:`MultiDict` copy."""
        new = MultiDict()
        new._data = {key: list(values) for key, values in self._data.items()}
        return new


class CaseInsensitiveDict:
    """A string-keyed mapping whose lookups ignore case."""

    def __init__(self, mapping=None):
        self._data = {}  # lower-key -> (original-key, value)
        if mapping:
            source = mapping.items() if hasattr(mapping, "items") else mapping
            for key, value in source:
                self[key] = value

    def __setitem__(self, key, value):
        self._data[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._data[key.lower()][1]

    def __delitem__(self, key):
        del self._data[key.lower()]

    def __contains__(self, key):
        return key.lower() in self._data

    def __iter__(self):
        return (original for original, _ in self._data.values())

    def __len__(self):
        return len(self._data)

    def get(self, key, default=None):
        item = self._data.get(key.lower())
        return item[1] if item is not None else default

    def items(self):
        return [(original, value) for original, value in self._data.values()]

    def keys(self):
        return [original for original, _ in self._data.values()]

    def values(self):
        return [value for _, value in self._data.values()]

    def copy(self):
        new = CaseInsensitiveDict()
        new._data = dict(self._data)
        return new

    def __repr__(self):
        return "CaseInsensitiveDict({!r})".format(self.items())
