#!/usr/bin/env python3
"""Reference oracle: match a path against a glob pattern per the task spec.

Semantics (full-string, anchored both ends):
  ?        one character that is not '/'
  *        zero or more characters, none of which is '/'
  **       zero or more characters, '/' included (a run of >=2 '*')
  [...]    one character in the class; ranges a-z; never matches '/'
  [!...]   / [^...]  negated class; also never matches '/'
  everything else is a literal; '/' matches '/'.

Memoized recursion on (pattern index, string index).
"""
import sys


def _class_match(cls: str, ch: str):
    """Does `ch` match character class body `cls` (the text between [ and ])?
    Returns None if the class is malformed (unterminated handled by caller)."""
    if ch == "/":
        return False
    negate = False
    i = 0
    if i < len(cls) and cls[i] in "!^":
        negate = True
        i += 1
    matched = False
    while i < len(cls):
        # range a-z: a char, a '-', then a char (with '-' not last)
        if i + 2 < len(cls) and cls[i + 1] == "-":
            lo, hi = cls[i], cls[i + 2]
            if lo <= ch <= hi:
                matched = True
            i += 3
        else:
            if cls[i] == ch:
                matched = True
            i += 1
    return matched != negate


def _match(p: str, s: str) -> bool:
    memo = {}

    def go(pi: int, si: int) -> bool:
        if (pi, si) in memo:
            return memo[(pi, si)]
        # end of pattern: match iff string also consumed
        if pi == len(p):
            res = si == len(s)
            memo[(pi, si)] = res
            return res
        c = p[pi]
        res = False
        if c == "*":
            # collapse a run of '*'; >=2 means globstar (crosses '/')
            j = pi
            while j < len(p) and p[j] == "*":
                j += 1
            star = "**" if (j - pi) >= 2 else "*"
            if star == "**":
                # match zero+ of ANY char
                k = si
                while True:
                    if go(j, k):
                        res = True
                        break
                    if k == len(s):
                        break
                    k += 1
            else:
                # match zero+ of non-'/'
                k = si
                while True:
                    if go(j, k):
                        res = True
                        break
                    if k == len(s) or s[k] == "/":
                        break
                    k += 1
        elif c == "?":
            res = si < len(s) and s[si] != "/" and go(pi + 1, si + 1)
        elif c == "[":
            close = p.find("]", pi + 2)  # +2 so a leading ']' is literal-ish; keep simple
            if close == -1:
                # unterminated '[' -> treat as a literal '['
                res = si < len(s) and s[si] == "[" and go(pi + 1, si + 1)
            else:
                body = p[pi + 1:close]
                res = si < len(s) and bool(_class_match(body, s[si])) and go(close + 1, si + 1)
        else:
            res = si < len(s) and s[si] == c and go(pi + 1, si + 1)
        memo[(pi, si)] = res
        return res

    return go(0, 0)


def main(argv):
    if len(argv) != 3:
        print("usage: glob_match.py <pattern> <path>", file=sys.stderr)
        return 2
    sys.stdout.write("true" if _match(argv[1], argv[2]) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
