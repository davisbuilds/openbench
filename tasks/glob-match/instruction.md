# Match a path against a glob pattern

Write a Python script named `glob_match.py` in this directory. Given a glob
pattern and a path, it prints whether the pattern matches the **entire** path.

## Usage

```
python3 glob_match.py <pattern> <path>
```

Print exactly `true` or `false` (lowercase, no trailing newline) to standard
output. The match is anchored at both ends: the pattern must consume the whole
path, not just a prefix.

## Pattern syntax

A path is a string that may contain `/` separators. Match these metacharacters;
every other character is a literal that matches only itself (and `/` matches
`/`).

- `?` — matches exactly **one** character, but **never** `/`.
- `*` — matches **zero or more** characters, **none of which may be `/`**. A
  single `*` therefore stays within one path segment.
- `**` — a run of **two or more** consecutive `*`. Matches **zero or more**
  characters, `/` **included**. This is the only construct that can cross
  segment boundaries. Treat it purely as "any characters including slashes" —
  it is **not** special-cased to collapse a `/`. So `a/**` matches `a/` and
  `a/b/c`, but `x/**/y` requires the literal `/y` to appear after whatever `**`
  consumed (e.g. it matches `x/p/y` and `x/y` is **false**, because after `x/`
  the `**` would match `` and then the literal `/y` has no slash to match).
- `[...]` — a character class: matches **one** character listed between the
  brackets. Supports ranges like `a-z` or `0-9`. A class **never** matches `/`,
  even if `/` appears inside the brackets.
- `[!...]` or `[^...]` — a **negated** class: matches one character that is
  **not** listed (and still never `/`).
- A `[` with no closing `]` is a literal `[`.

## Examples

```
python3 glob_match.py '*.py' 'main.py'        ->  true
python3 glob_match.py '*.py' 'src/main.py'    ->  false   (* cannot cross '/')
python3 glob_match.py '**/*.py' 'src/main.py' ->  true
python3 glob_match.py '[a-c]?' 'bx'           ->  true
python3 glob_match.py '[!0-9]' '/'            ->  false   (class never matches '/')
```

Done when `glob_match.py` prints the correct verdict for every pattern/path.
