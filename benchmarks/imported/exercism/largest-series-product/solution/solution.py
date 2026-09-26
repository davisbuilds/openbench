"""Reference solution: largest series product."""


def largest_product(digits, span):
    if span < 0:
        raise ValueError("span must not be negative")
    if span > len(digits):
        raise ValueError("span must be smaller than string length")
    if any(not ch.isdigit() for ch in digits):
        raise ValueError("digits input must only contain digits")

    best = 0
    for i in range(len(digits) - span + 1):
        product = 1
        for ch in digits[i:i + span]:
            product *= int(ch)
        best = max(best, product)
    return best
