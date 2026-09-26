#!/usr/bin/env python3
"""Reference oracle: canonicalize a JSON document per the task spec.

This is the intended-correct implementation. The graded checker compares an
agent's canon.py output against expected files that were generated FROM this
file, so the oracle and the fixtures are self-consistent by construction.
"""
import sys
import json

# Control characters that get a short two-char escape; everything else below
# 0x20 becomes \u00xx (lowercase hex). 0x20 and above is emitted literally
# (as UTF-8), including non-ASCII -- no gratuitous \u escaping.
_SHORT = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _encode_string(s: str) -> str:
    out = ["\""]
    for ch in s:
        cp = ord(ch)
        if cp in _SHORT:
            out.append(_SHORT[cp])
        elif cp < 0x20:
            out.append("\\u%04x" % cp)
        else:
            out.append(ch)  # literal, incl. '/' and non-ASCII
    out.append("\"")
    return "".join(out)


def _encode_number(n) -> str:
    # Spec restricts numbers to integers. Normalize -0 to 0; no '+', no leading
    # zeros (int() already guarantees that), no decimal point.
    if isinstance(n, bool):  # bool is a subclass of int -- guard it
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    # A float that is integral (e.g. 5.0) collapses to its integer form; any
    # non-integral float is out of spec for the fixtures but handled defensively.
    if n == int(n):
        return str(int(n))
    return repr(n)


def canon(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _encode_number(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canon(v) for v in value) + "]"
    if isinstance(value, dict):
        # Keys sorted ascending by Unicode code point (Python's default string
        # ordering IS code-point order).
        items = sorted(value.items(), key=lambda kv: kv[0])
        return "{" + ",".join(_encode_string(k) + ":" + canon(v) for k, v in items) + "}"
    raise TypeError("unsupported value: %r" % (value,))


def main(argv):
    if len(argv) != 2:
        print("usage: canon.py <file.json>", file=sys.stderr)
        return 2
    with open(argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    sys.stdout.write(canon(data))  # no trailing newline
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
