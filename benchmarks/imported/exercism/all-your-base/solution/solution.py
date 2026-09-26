"""Reference solution: arbitrary base conversion."""


def rebase(input_base, digits, output_base):
    if input_base < 2:
        raise ValueError("input base must be >= 2")
    if output_base < 2:
        raise ValueError("output base must be >= 2")
    if any(d < 0 or d >= input_base for d in digits):
        raise ValueError("all digits must satisfy 0 <= d < input base")

    value = 0
    for d in digits:
        value = value * input_base + d

    out = []
    while value > 0:
        out.append(value % output_base)
        value //= output_base
    return out[::-1] if out else [0]
