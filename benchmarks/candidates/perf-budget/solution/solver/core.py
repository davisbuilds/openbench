"""Rank analytics: "smaller elements after self".

``count_smaller_after(values)`` returns a list ``res`` of the same length as
``values`` where::

    res[i] = number of indices j with  j > i  and  values[j] < values[i]

i.e. for each element, how many strictly-smaller elements appear to its right.

This reference implementation replaces the naive O(n^2) pair scan with an
O(n log n) sweep: values are coordinate-compressed to dense ranks, then a
Fenwick tree (binary indexed tree) is filled right-to-left. When element ``i``
is processed, the tree already holds every element to its right, so a prefix
query over ranks strictly below ``values[i]`` yields ``res[i]`` directly.
"""
from __future__ import annotations

from typing import List


def count_smaller_after(values: List[int]) -> List[int]:
    n = len(values)
    if n == 0:
        return []

    # Coordinate-compress to dense 1-based ranks (ties share a rank).
    order = sorted(set(values))
    rank = {v: i + 1 for i, v in enumerate(order)}
    size = len(order)

    tree = [0] * (size + 1)
    res = [0] * n

    for i in range(n - 1, -1, -1):
        r = rank[values[i]]
        # Query: how many already-seen (to the right) have rank < r.
        idx = r - 1
        acc = 0
        while idx > 0:
            acc += tree[idx]
            idx -= idx & (-idx)
        res[i] = acc
        # Update: record this element at its rank.
        idx = r
        while idx <= size:
            tree[idx] += 1
            idx += idx & (-idx)

    return res
