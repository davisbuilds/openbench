# Atbash cipher

Implement the Atbash substitution cipher, which maps each Latin letter to its
mirror in the alphabet: `a`↔`z`, `b`↔`y`, … `m`↔`n`. Digits pass through
unchanged; every other character (spaces, punctuation) is dropped.

**Encoding** lowercases the input, removes non-alphanumeric characters, applies
the mirror mapping to letters, and then groups the output into runs of five
characters separated by single spaces. For example `"test"` → `"gvhg"` and
`"Testing, 1 2 3, testing."` → `"gvhgr mt123 gvhgr mt"`.

**Decoding** reverses a ciphertext: strip its spaces, apply the same mirror
mapping (letters map back, digits unchanged), and return the result with no
grouping. Decoding `"gvhgr mt123 gvhgr mt"` gives `"testing123testing"`. The
letter mapping is its own inverse, so the same substitution serves both
directions.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `encode(phrase)` — return the grouped Atbash ciphertext of `phrase`
- `decode(phrase)` — return the plaintext for Atbash ciphertext `phrase`

Done when `solution.py` implements the interface above exactly as specified.
