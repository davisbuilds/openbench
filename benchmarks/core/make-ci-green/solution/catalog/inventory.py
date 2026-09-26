"""In-memory stock tracking for the catalog."""


class Inventory:
    def __init__(self):
        self._stock = {}
        self._history = []

    def add(self, book_id, count):
        self._stock[book_id] = self._stock.get(book_id, 0) + count
        self._history.append(("add", book_id, count))

    def available(self, book_id):
        return self._stock.get(book_id, 0)

    def checkout(self, book_id):
        if self.available(book_id) <= 0:
            raise ValueError("no copies available: {}".format(book_id))
        self._stock[book_id] -= 1
        self._history.append(("checkout", book_id, 1))

    def recent(self, n):
        """Return the last ``n`` history events, most recent last."""
        return self._history[-n:]
