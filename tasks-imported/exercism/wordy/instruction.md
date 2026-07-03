# Word-problem calculator

Parse and evaluate a spoken-style arithmetic question and return the integer
answer.

Questions start with `"What is"` and end with a `?`. Between them is a number
followed by zero or more operations, each an operator word and another number,
applied strictly left to right (no precedence): `"What is 3 plus 2 multiplied
by 3?"` is `(3 + 2) * 3 = 15`, not `9`.

The four operations are `plus`, `minus`, `multiplied by`, and `divided by`.
Numbers may be negative. `"What is 5?"` is `5`.

Raise a `ValueError` for anything that is not a well-formed sum: a non-math
question, an unsupported operation (e.g. `"cubed"`), two numbers in a row with
no operator between them, or two operators in a row.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `answer(question)` — return the integer value of the word problem `question`

Raise `ValueError` for a malformed or non-math question.

Done when `solution.py` implements the interface above exactly as specified.
