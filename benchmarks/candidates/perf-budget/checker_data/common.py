"""Checker-owned input generation and output fingerprinting.

Lives OUTSIDE the agent's workspace and is loaded by both the parent scorer
(``run_score.py``) and the per-case subprocess (``run_tier.py``) via an
absolute path, so the agent cannot shadow it from the workspace. Given the same
``(seed, n, mode)`` it deterministically produces the same input list, and it
fingerprints an output list identically on both sides so results can be
compared without shipping megabytes of integers over a pipe.
"""
import hashlib
import random


def generate(seed, n, mode):
    """Deterministically build an input list of length ``n``.

    ``mode`` selects a distribution/structure so the suite exercises more than
    one shape of input (uniform, heavy ties, monotone, negatives, ...).
    """
    r = random.Random(seed)
    if n == 0:
        return []
    if mode == "uniform":
        return [r.randint(0, n) for _ in range(n)]
    if mode == "wide":
        return [r.randint(-10**9, 10**9) for _ in range(n)]
    if mode == "constant":
        return [r.randint(-1000, 1000)] * n
    if mode == "sorted":
        return list(range(n))
    if mode == "reverse":
        return list(range(n - 1, -1, -1))
    if mode == "fewvals":
        return [r.randint(0, 5) for _ in range(n)]
    if mode == "negatives":
        return [r.randint(-n, 0) for _ in range(n)]
    raise ValueError("unknown mode: %r" % (mode,))


def digest(res):
    """Order-sensitive fingerprint of an output list of ints."""
    h = hashlib.sha256()
    h.update(len(res).to_bytes(8, "little"))
    for x in res:
        h.update(int(x).to_bytes(16, "little", signed=True))
    return h.hexdigest()
