# Spreadsheet formula engine

Implement a small spreadsheet evaluator in `engine.py`, exposing one function:

```python
def evaluate(cells):
    ...
```

`cells` maps a cell name (an uppercase column label followed by a row number,
e.g. `"A1"`, `"B12"`) to its **raw contents** as a string. Return a new dict
mapping every one of those same cell names to its **computed value**.

## Cell contents

A cell's contents are one of:

- **Empty** (`""`) — evaluates to the number `0`.
- **A number literal** — an integer like `"42"` or a decimal like `"1.5"`.
- **A formula** — a string beginning with `=`, e.g. `"=A1+B2*3"`.

## Formula grammar

After the leading `=`, a formula is an arithmetic expression over:

- number literals (integer or decimal),
- cell references (evaluated by looking up that cell's computed value),
- the binary operators `+`, `-`, `*`, `/` with the usual precedence (`*` and `/`
  bind tighter than `+` and `-`) and left-to-right associativity,
- unary minus, e.g. `=-A1`,
- parentheses for grouping.

Division uses real (floating-point) division, so `=7/2` is `3.5`. A reference to
a cell that is empty or absent from the input evaluates to `0`.

## Error values

Two conditions produce a **string** result instead of a number, and any formula
that references such a cell yields the same string (errors propagate):

- **`"#DIV/0"`** — division by zero.
- **`"#CYCLE"`** — the cell is part of a reference cycle (it depends, directly
  or transitively, on itself).

## Example

```python
evaluate({"A1": "10", "A2": "20", "B1": "=A1+A2", "B2": "=B1/2"})
# -> {"A1": 10, "A2": 20, "B1": 30, "B2": 15.0}

evaluate({"A1": "=B1", "B1": "=A1"})
# -> {"A1": "#CYCLE", "B1": "#CYCLE"}
```

Done when `evaluate` computes every cell according to the rules above.
