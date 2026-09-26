# Spiral matrix

Build an `size` x `size` matrix holding the numbers `1` through `size*size`,
placed by walking the grid in an inward clockwise spiral.

Start at the top-left cell with `1` and move right along the top row, then down
the right side, then left along the bottom, then up, spiralling inward and
incrementing at each step until the grid is full. Return the matrix as a list of
rows (each row a list of ints). A `size` of `0` gives `[]`.

For `size = 3` the result is:

```
[[1, 2, 3],
 [8, 9, 4],
 [7, 6, 5]]
```

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `spiral_matrix(size)` — return the `size` x `size` clockwise spiral matrix

Done when `solution.py` implements the interface above exactly as specified.
