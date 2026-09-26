# Run-length encoding

Implement a simple run-length codec for text.

**Encoding** replaces each run of two or more identical consecutive characters
with the run length written in decimal followed by that single character. A run
of length one is left as the bare character (never `1X`). So `AAABBBCCCC`
encodes to `3A3B4C`, and `aaAA` encodes to `2a2A` (case is significant). Spaces
count as ordinary characters: `"  hsqq qww  "` encodes to `2 hs2q q2w2 `.

**Decoding** is the exact inverse: a run of digits gives the repeat count for
the character that immediately follows it, and a character with no preceding
digits appears once. Decoding `3A3B4C` yields `AAABBBCCCC`.

The input to encoding contains only letters (upper- and lowercase) and spaces —
never digits — so encoded output is always unambiguous. Encoding then decoding
any valid input must return it unchanged.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `encode(string)` — return the run-length encoding of `string`
- `decode(string)` — return the text that `string` encodes

Done when `solution.py` implements the interface above exactly as specified.
