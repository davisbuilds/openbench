"""Reference solution: Luhn checksum."""


def valid(value):
    stripped = value.replace(" ", "")
    if len(stripped) <= 1 or not stripped.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(stripped)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
