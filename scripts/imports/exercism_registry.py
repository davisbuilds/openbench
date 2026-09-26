"""Curation registry for the Exercism import tier.

``convert_exercism.py`` fetches each exercise's *canonical data* (the test
cases) and *description* straight from the upstream MIT-licensed
``exercism/problem-specifications`` repo, but it never reuses upstream prose or
any track's solution. The task-facing text and the interface contract live
here, written from scratch:

  - ``title``     one-line heading for instruction.md
  - ``prose``     the engineering request, in our own words. It is the ONLY
                  thing the agent sees besides the workspace stub, so it must be
                  a complete, self-contained spec. It never mentions Exercism,
                  the checker, the solution, or that this is a benchmark.
  - ``functions`` maps each canonical ``property`` name to the function the
                  agent must expose in ``solution.py``: its snake_case ``name``,
                  ordered ``params`` (snake_case), and a one-line ``doc``. The
                  converter asserts these params cover the input keys actually
                  present in the canonical data, so a drift upstream is caught
                  at convert time rather than silently mis-scored.
  - ``roundtrip`` optional: maps a canonical property that is a *composition*
                  (Exercism's "consistency" round-trip cases) to the pair of
                  function names to chain, ``[outer_after, inner_first]`` read
                  as ``outer(inner(**input))``.
  - ``raises``    optional note appended to the interface section telling the
                  agent to raise ``ValueError`` for the invalid inputs the spec
                  describes (only for exercises whose canonical data has error
                  cases).

Reference solutions are NOT here and are NOT generated: they are written by
hand, per task, under ``solution/solution.py``.
"""

REGISTRY = {
    # ---- prototype (mid difficulty) --------------------------------------
    "run-length-encoding": {
        "title": "Run-length encoding",
        "prose": """\
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
any valid input must return it unchanged.""",
        "functions": {
            "encode": {"name": "encode", "params": ["string"],
                       "doc": "return the run-length encoding of `string`"},
            "decode": {"name": "decode", "params": ["string"],
                       "doc": "return the text that `string` encodes"},
        },
        "roundtrip": {"consistency": ["decode", "encode"]},
    },

    # ---- batch (harder end) ----------------------------------------------
    "luhn": {
        "title": "Luhn checksum validator",
        "prose": """\
Validate a number string against the Luhn formula, the check used by credit
card numbers and many other identifiers.

Given a string that may contain spaces, decide whether it is valid:

1. Strings of length 1 or less, or strings containing any character other than
   digits and spaces, are **not** valid.
2. Otherwise strip the spaces. Walking the remaining digits from right to left,
   double every second digit (the 2nd, 4th, … from the right). If a doubled
   value exceeds 9, subtract 9 from it.
3. The number is valid exactly when the sum of the resulting digits is a
   multiple of 10.

For example `"4539 3195 0343 6467"` is valid, while `"8273 1232 7352 0569"` is
not. `"0"` is not valid (too short); `"0 0 0"` is valid.""",
        "functions": {
            "valid": {"name": "valid", "params": ["value"],
                      "doc": "return True iff `value` passes the Luhn check"},
        },
    },

    "roman-numerals": {
        "title": "Roman numeral converter",
        "prose": """\
Convert a positive integer (1 through 3999) to its Roman numeral string.

Roman numerals combine these symbols, largest value first: `M`=1000, `D`=500,
`C`=100, `L`=50, `X`=10, `V`=5, `I`=1. A symbol may repeat up to three times to
add its value. Instead of four repeats, subtractive pairs are used: `IV`=4,
`IX`=9, `XL`=40, `XC`=90, `CD`=400, `CM`=900.

Build the numeral by taking as many of each value as fit, high to low, using the
subtractive pairs where they apply. Examples: 4 → `IV`, 14 → `XIV`, 49 → `XLIX`,
1990 → `MCMXC`, 2008 → `MMVIII`, 3888 → `MMMDCCCLXXXVIII`.""",
        "functions": {
            "roman": {"name": "roman", "params": ["number"],
                      "doc": "return the Roman numeral for `number`"},
        },
    },

    "atbash-cipher": {
        "title": "Atbash cipher",
        "prose": """\
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
directions.""",
        "functions": {
            "encode": {"name": "encode", "params": ["phrase"],
                       "doc": "return the grouped Atbash ciphertext of `phrase`"},
            "decode": {"name": "decode", "params": ["phrase"],
                       "doc": "return the plaintext for Atbash ciphertext `phrase`"},
        },
    },

    "matching-brackets": {
        "title": "Balanced brackets",
        "prose": """\
Decide whether every bracket in a string is correctly matched and nested.

The bracket characters are `()`, `[]` and `{}`. A string is balanced when each
opening bracket is closed by the matching kind, closings happen in the reverse
order of their openings, and nothing is left open or closed unexpectedly. Any
non-bracket characters are ignored.

`"{[()]}"` and `"{ ([]) }"` are balanced; `"{[)][]}"`, `"([)]"` and `"["` are
not. The empty string is balanced.""",
        "functions": {
            "isPaired": {"name": "is_paired", "params": ["value"],
                         "doc": "return True iff the brackets in `value` are balanced"},
        },
    },

    "pig-latin": {
        "title": "Pig Latin translator",
        "prose": """\
Translate English text to Pig Latin. The input is one or more lowercase words
separated by single spaces; translate each word independently and rejoin them
with single spaces.

Apply the first rule that matches a word:

1. If it begins with a vowel sound — a leading `a`, `e`, `i`, `o`, `u`, or the
   clusters `xr` or `yt` — append `"ay"` with nothing moved: `apple` → `appleay`,
   `xray` → `xrayay`.
2. Otherwise move the leading run of consonants to the end and append `"ay"`:
   `pig` → `igpay`, `chair` → `airchay`, `thrush` → `ushthray`.
3. Treat `qu` as a single consonant unit that moves together: `quick` →
   `ickquay`, `square` → `aresquay`.
4. When `y` follows the leading consonants, it acts as the vowel that stops the
   run: `my` → `ymay`, `rhythm` → `ythmrhay`. A leading `y` is instead treated
   as a consonant: `yellow` → `ellowyay`.""",
        "functions": {
            "translate": {"name": "translate", "params": ["phrase"],
                          "doc": "return the Pig Latin translation of `phrase`"},
        },
    },

    "wordy": {
        "title": "Word-problem calculator",
        "prose": """\
Parse and evaluate a spoken-style arithmetic question and return the integer
answer.

Questions start with `"What is"` and end with a `?`. Between them is a number
followed by zero or more operations, each an operator word and another number,
applied strictly left to right (no precedence): `"What is 3 plus 2 multiplied
by 3?"` is `(3 + 2) * 3 = 15`, not `9`.

The four operations are `plus`, `minus`, `multiplied by`, and `divided by`.
Numbers may be negative. `"What is 5?"` is `5`.

Raise a `ValueError` for anything that is not a well-formed sum: a non-math
question, an unsupported operation (e.g. `"cubed"`), two numbers in a row with
no operator between them, or two operators in a row.""",
        "functions": {
            "answer": {"name": "answer", "params": ["question"],
                       "doc": "return the integer value of the word problem `question`"},
        },
        "raises": "Raise `ValueError` for a malformed or non-math question.",
    },

    "phone-number": {
        "title": "North American phone-number cleaner",
        "prose": """\
Clean up user-entered North American (NANP) phone numbers into ten digits.

The input may contain digits, spaces, and the punctuation `+`, `-`, `.`, `(`,
`)`. Strip all of that and return the ten-digit number as a string, e.g.
`"(223) 456-7890"` → `"2234567890"`.

An input with exactly eleven digits is accepted only when the leading digit is
`1` (the country code), which is removed: `"1 (223) 456-7890"` → `"2234567890"`.

Raise a `ValueError` if the result is not a valid NANP number: fewer or more
than the expected digits; an eleven-digit number whose country code is not `1`;
any letters or disallowed punctuation; or an area code (first digit of the ten)
or exchange code (fourth digit) that is `0` or `1`.""",
        "functions": {
            "clean": {"name": "clean", "params": ["phrase"],
                      "doc": "return the ten-digit form of `phrase`"},
        },
        "raises": "Raise `ValueError` for any input that is not a valid NANP number.",
    },

    "largest-series-product": {
        "title": "Largest series product",
        "prose": """\
Given a string of digits, find the largest product obtainable from any run of
`span` adjacent digits.

For `digits="63915"` and `span=3` the candidate runs are `639`, `391`, `915`;
their products are 162, 27, 45, so the answer is 162. A `span` of `0` yields the
empty product, `1`.

Raise a `ValueError` when the request is impossible or ill-defined: a `span`
larger than the number of digits; a negative `span`; or an input string that
contains any non-digit character (spaces or letters included).""",
        "functions": {
            "largestProduct": {"name": "largest_product", "params": ["digits", "span"],
                               "doc": "return the largest product of any `span` adjacent digits of `digits`"},
        },
        "raises": "Raise `ValueError` for an invalid span or a non-digit input.",
    },

    "all-your-base": {
        "title": "Arbitrary base conversion",
        "prose": """\
Convert a number between arbitrary positional bases.

The number is given as `digits`, a list of its digit values from most
significant to least, in `input_base`. Return the same number as a list of digit
values in `output_base`, again most significant first. For example the digits
`[1, 0, 1]` in base 2 (i.e. five) become `[1, 2]` in base 3. Zero is written as
the single digit `[0]` in every base, so all-zero and empty inputs convert to
`[0]`.

Raise a `ValueError` when the conversion is not well defined: an `input_base` or
`output_base` less than 2, or any element of `digits` that is negative or not
less than `input_base`.""",
        "functions": {
            "rebase": {"name": "rebase", "params": ["input_base", "digits", "output_base"],
                       "doc": "return `digits` (base `input_base`) rewritten in `output_base`"},
        },
        "raises": "Raise `ValueError` for an out-of-range base or an invalid digit.",
    },

    "spiral-matrix": {
        "title": "Spiral matrix",
        "prose": """\
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
```""",
        "functions": {
            "spiralMatrix": {"name": "spiral_matrix", "params": ["size"],
                             "doc": "return the `size` x `size` clockwise spiral matrix"},
        },
    },
}
