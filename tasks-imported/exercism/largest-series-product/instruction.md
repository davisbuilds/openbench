# Largest series product

Given a string of digits, find the largest product obtainable from any run of
`span` adjacent digits.

For `digits="63915"` and `span=3` the candidate runs are `639`, `391`, `915`;
their products are 162, 27, 45, so the answer is 162. A `span` of `0` yields the
empty product, `1`.

Raise a `ValueError` when the request is impossible or ill-defined: a `span`
larger than the number of digits; a negative `span`; or an input string that
contains any non-digit character (spaces or letters included).

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `largest_product(digits, span)` — return the largest product of any `span` adjacent digits of `digits`

Raise `ValueError` for an invalid span or a non-digit input.

Done when `solution.py` implements the interface above exactly as specified.
