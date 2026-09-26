# Make the ranking counter fast on large inputs

Our analytics pipeline has a small hot function in `solver/core.py`:

```python
count_smaller_after(values: list[int]) -> list[int]
```

For a list `values`, it returns a list `res` of the same length where

```
res[i] = number of indices j with j > i and values[j] < values[i]
```

i.e. for each element, how many strictly-smaller elements sit to its right.
See `README.md` for the full contract and an example.

The current implementation is correct but was written as a direct pair scan,
and it has become a bottleneck: on the larger reports (hundreds of thousands to
a few million elements) it is far too slow and now dominates the pipeline's
runtime.

## What we need

Make `count_smaller_after` fast enough to handle inputs up to about
**1,000,000 elements** quickly (well under a couple of seconds at that size on
a normal machine), while scaling smoothly through the smaller sizes too.

Hard requirements:

- **The output must not change.** For every input, the returned list must be
  exactly what the current function returns — same values, same order. Ties
  (`values[j] == values[i]`) are not counted; only strictly smaller.
- Handle any `int` values, including negatives and heavy repeats, and the empty
  list.
- Keep it dependency-free: standard-library Python only (the pipeline runs on a
  bare interpreter with nothing installed).

Only `solver/core.py` needs to change, but you may reorganize within the
`solver/` package if you prefer. The public import
`from solver import count_smaller_after` must keep working.
