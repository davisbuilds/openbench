# North American phone-number cleaner

Clean up user-entered North American (NANP) phone numbers into ten digits.

The input may contain digits, spaces, and the punctuation `+`, `-`, `.`, `(`,
`)`. Strip all of that and return the ten-digit number as a string, e.g.
`"(223) 456-7890"` → `"2234567890"`.

An input with exactly eleven digits is accepted only when the leading digit is
`1` (the country code), which is removed: `"1 (223) 456-7890"` → `"2234567890"`.

Raise a `ValueError` if the result is not a valid NANP number: fewer or more
than the expected digits; an eleven-digit number whose country code is not `1`;
any letters or disallowed punctuation; or an area code (first digit of the ten)
or exchange code (fourth digit) that is `0` or `1`.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `clean(phrase)` — return the ten-digit form of `phrase`

Raise `ValueError` for any input that is not a valid NANP number.

Done when `solution.py` implements the interface above exactly as specified.
