# Arbitrary base conversion

Convert a number between arbitrary positional bases.

The number is given as `digits`, a list of its digit values from most
significant to least, in `input_base`. Return the same number as a list of digit
values in `output_base`, again most significant first. For example the digits
`[1, 0, 1]` in base 2 (i.e. five) become `[1, 2]` in base 3. Zero is written as
the single digit `[0]` in every base, so all-zero and empty inputs convert to
`[0]`.

Raise a `ValueError` when the conversion is not well defined: an `input_base` or
`output_base` less than 2, or any element of `digits` that is negative or not
less than `input_base`.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `rebase(input_base, digits, output_base)` — return `digits` (base `input_base`) rewritten in `output_base`

Raise `ValueError` for an out-of-range base or an invalid digit.

Done when `solution.py` implements the interface above exactly as specified.
