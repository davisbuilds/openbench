"""Library members and borrowing limits."""

# The lending policy allows each member to hold five books at once.
MAX_BORROWED = 5


class Member:
    def __init__(self, name):
        self.name = name
        self.borrowed = []

    def can_borrow(self):
        return len(self.borrowed) < MAX_BORROWED

    def borrow(self, book_id):
        if not self.can_borrow():
            raise ValueError("borrow limit reached")
        self.borrowed.append(book_id)

    def return_book(self, book_id):
        self.borrowed.remove(book_id)
