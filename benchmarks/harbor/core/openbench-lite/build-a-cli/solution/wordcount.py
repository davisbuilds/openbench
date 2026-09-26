#!/usr/bin/env python3
"""Print the N most common words in a text file."""
import re
import sys
from collections import Counter


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: wordcount.py <filename> <N>\n")
        return 2

    filename = argv[1]
    try:
        n = int(argv[2])
    except ValueError:
        sys.stderr.write("N must be an integer\n")
        return 2
    if n < 0:
        sys.stderr.write("N must be non-negative\n")
        return 2

    with open(filename, "r", encoding="utf-8") as f:
        text = f.read()

    words = re.findall(r"[a-z0-9]+", text.lower())
    counts = Counter(words)

    # Sort by count descending, then word ascending for ties.
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    out = []
    for word, count in ordered[:n]:
        out.append("{} {}".format(word, count))
    sys.stdout.write("".join(line + "\n" for line in out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
