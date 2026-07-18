"""Independent ground-truth oracle: merge-sort counting.

This deliberately uses a DIFFERENT algorithm from the reference solution (which
uses a Fenwick tree) so the grader's notion of "correct" does not share an
implementation with any expected answer. Both are O(n log n); this one counts
"smaller elements after self" during the merge step. It is validated against a
brute-force O(n^2) pass on small inputs inside ``run_score.py`` on every run.
"""


def count_smaller_after(values):
    n = len(values)
    if n == 0:
        return []
    res = [0] * n
    idx = list(range(n))

    def sort(lo, hi):
        if hi - lo <= 1:
            return
        mid = (lo + hi) // 2
        sort(lo, mid)
        sort(mid, hi)
        tmp = []
        i, j = lo, mid
        smaller = 0  # right-half items already emitted (they are < values[idx[i]])
        while i < mid and j < hi:
            if values[idx[j]] < values[idx[i]]:
                smaller += 1
                tmp.append(idx[j])
                j += 1
            else:
                res[idx[i]] += smaller
                tmp.append(idx[i])
                i += 1
        while i < mid:
            res[idx[i]] += smaller
            tmp.append(idx[i])
            i += 1
        while j < hi:
            tmp.append(idx[j])
            j += 1
        idx[lo:hi] = tmp

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 4 * n + 100))
    try:
        sort(0, n)
    finally:
        sys.setrecursionlimit(old)
    return res


def brute_force(values):
    """O(n^2) reference used only to self-check the oracle on tiny inputs."""
    n = len(values)
    res = [0] * n
    for i in range(n):
        vi = values[i]
        res[i] = sum(1 for j in range(i + 1, n) if values[j] < vi)
    return res
