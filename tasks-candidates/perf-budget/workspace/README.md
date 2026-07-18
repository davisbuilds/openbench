# solver

A single small module used by our analytics ranking report.

## API

```python
from solver import count_smaller_after

count_smaller_after(values: list[int]) -> list[int]
```

For an input list `values`, returns a list `res` of the same length where

```
res[i] = number of indices j with j > i and values[j] < values[i]
```

— for each element, how many strictly-smaller elements appear to its right.

```python
>>> count_smaller_after([5, 2, 6, 1])
[2, 1, 1, 0]
```

The output must be **exactly** this list of counts, in order. Ties
(`values[j] == values[i]`) are **not** counted — only strictly smaller.

Inputs may contain any Python `int` (including negatives and repeats) and may
be very large.
