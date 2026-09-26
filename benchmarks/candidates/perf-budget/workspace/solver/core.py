"""Rank analytics: "smaller elements after self".

``count_smaller_after(values)`` returns a list ``res`` of the same length as
``values`` where::

    res[i] = number of indices j with  j > i  and  values[j] < values[i]

i.e. for each element, how many strictly-smaller elements appear to its right.

Example::

    >>> count_smaller_after([5, 2, 6, 1])
    [2, 1, 1, 0]

    # 5 has {2, 1} to its right that are smaller -> 2
    # 2 has {1}     to its right that are smaller -> 1
    # 6 has {1}     to its right that are smaller -> 1
    # 1 has nothing to its right                  -> 0

This feeds a ranking/ordering report in our analytics pipeline. The
implementation below is a direct, obviously-correct translation of the
definition.
"""
from __future__ import annotations

from typing import List


def count_smaller_after(values: List[int]) -> List[int]:
    """Count, for each position, the strictly-smaller elements to its right.

    Compares every pair (i, j) with j > i. Correct for all inputs, but the
    work grows with the square of the input length.
    """
    n = len(values)
    res = [0] * n
    for i in range(n):
        vi = values[i]
        count = 0
        for j in range(i + 1, n):
            if values[j] < vi:
                count += 1
        res[i] = count
    return res
