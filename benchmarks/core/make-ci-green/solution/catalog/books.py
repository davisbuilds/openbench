"""Book records for the catalog."""


class Book:
    def __init__(self, title, author, year, copies):
        self.title = title
        self.author = author
        self.year = year
        self.copies = copies

    def age(self, current_year):
        """Age of the book in years as of ``current_year``."""
        return current_year - self.year

    def __repr__(self):
        return "Book({!r}, {!r}, {!r})".format(self.title, self.author, self.year)


def parse_book(line):
    """Parse a ``Title|Author|Year|Copies`` record into a :class:`Book`."""
    title, author, year, copies = line.split("|")
    return Book(title.strip(), author.strip(), int(year), int(copies))
