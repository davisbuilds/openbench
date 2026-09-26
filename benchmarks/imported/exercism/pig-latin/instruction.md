# Pig Latin translator

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
   as a consonant: `yellow` → `ellowyay`.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `translate(phrase)` — return the Pig Latin translation of `phrase`

Done when `solution.py` implements the interface above exactly as specified.
