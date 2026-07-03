# Balanced brackets

Decide whether every bracket in a string is correctly matched and nested.

The bracket characters are `()`, `[]` and `{}`. A string is balanced when each
opening bracket is closed by the matching kind, closings happen in the reverse
order of their openings, and nothing is left open or closed unexpectedly. Any
non-bracket characters are ignored.

`"{[()]}"` and `"{ ([]) }"` are balanced; `"{[)][]}"`, `"([)]"` and `"["` are
not. The empty string is balanced.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `is_paired(value)` — return True iff the brackets in `value` are balanced

Done when `solution.py` implements the interface above exactly as specified.
