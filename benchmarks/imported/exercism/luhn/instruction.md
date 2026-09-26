# Luhn checksum validator

Validate a number string against the Luhn formula, the check used by credit
card numbers and many other identifiers.

Given a string that may contain spaces, decide whether it is valid:

1. Strings of length 1 or less, or strings containing any character other than
   digits and spaces, are **not** valid.
2. Otherwise strip the spaces. Walking the remaining digits from right to left,
   double every second digit (the 2nd, 4th, … from the right). If a doubled
   value exceeds 9, subtract 9 from it.
3. The number is valid exactly when the sum of the resulting digits is a
   multiple of 10.

For example `"4539 3195 0343 6467"` is valid, while `"8273 1232 7352 0569"` is
not. `"0"` is not valid (too short); `"0 0 0"` is valid.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `valid(value)` — return True iff `value` passes the Luhn check

Done when `solution.py` implements the interface above exactly as specified.
