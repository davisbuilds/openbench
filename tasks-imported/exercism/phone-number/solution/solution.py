"""Reference solution: North American phone-number cleaner."""

ALLOWED = set("0123456789 +-.()")


def clean(phrase):
    if any(ch not in ALLOWED for ch in phrase):
        raise ValueError("contains invalid characters")
    digits = "".join(ch for ch in phrase if ch.isdigit())
    if len(digits) == 11:
        if digits[0] != "1":
            raise ValueError("11 digits must start with 1")
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("incorrect number of digits")
    if digits[0] in "01":
        raise ValueError("area code cannot start with zero or one")
    if digits[3] in "01":
        raise ValueError("exchange code cannot start with zero or one")
    return digits
