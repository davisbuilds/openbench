# Canonicalize a JSON document

Write a Python script named `canon.py` in this directory. Given a path to a
UTF-8 JSON file, it prints a single **canonical** serialization of that document
to standard output.

## Usage

```
python3 canon.py <file.json>
```

Print the canonical form to stdout with **no trailing newline** and **no
insignificant whitespace** anywhere outside of string values.

The input is always a valid JSON document. Numbers in the input are always
integers (no fractions or exponents). Object keys within a single object are
always unique.

## Canonical form

Produce output that is byte-for-byte determined by the following rules.

### Structure
- No spaces, tabs, or newlines except inside string values. Arrays are
  `[a,b,c]`, objects are `{"k":v,"m":w}` — commas and the `:` have no
  surrounding whitespace.
- **Array element order is preserved** exactly as in the input.
- **Object members are sorted by key**, ascending, comparing keys by Unicode
  code point (the natural code-point ordering of the key strings). For example
  the keys `"A"`, `"a"`, `"ab"`, `"b"` sort in that order, because `A` (U+0041)
  precedes `a` (U+0061).

### Strings
Emit `"` ... `"` and inside, escape exactly these and nothing else:
- `"` → `\"` and `\` → `\\`.
- The control characters that have a short escape: backspace → `\b`,
  tab → `\t`, line feed → `\n`, form feed → `\f`, carriage return → `\r`.
- Any **other** character below U+0020 → `\u00xx`, using **lowercase**
  hexadecimal (e.g. U+0001 → `\u0001`, U+001F → `\u001f`).
- Every character U+0020 and above is emitted **literally as UTF-8** — including
  `/` (which is *not* escaped) and all non-ASCII characters (e.g. `é`, emoji).
  Do not emit `\uXXXX` for anything at or above U+0020.

### Numbers
- Emit integers in decimal with no leading zeros, no `+` sign, and no decimal
  point. Negative zero (`-0`) is emitted as `0`.

### Literals
- `true`, `false`, and `null` are emitted as those three literals.

## Example

Input:

```json
{ "url": "http://a/b", "n": 5, "a": [3, 1, 2] }
```

Output (single line, no trailing newline):

```
{"a":[3,1,2],"n":5,"url":"http://a/b"}
```

Done when `canon.py` emits the exact canonical form for every input.
