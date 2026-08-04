# Build a word-frequency command-line tool

Write a Python script named `wordcount.py` in this directory. It reads a text
file and prints the most common words it contains.

## Usage

```
python3 wordcount.py <filename> <N>
```

- `<filename>` — path to a UTF-8 text file to analyze.
- `<N>` — a non-negative integer: how many of the top words to print.

## What counts as a word

Lowercase the entire file, then treat every maximal run of the characters
`a`–`z` and `0`–`9` as one word. Every other character (spaces, punctuation,
newlines, etc.) is only a separator. For example the text `Hello, hello WORLD!`
contains the words `hello`, `hello`, `world`.

## Output

Print the `N` most frequent words, one per line, as:

```
<word> <count>
```

the word and the count separated by a single space, each line ending with a
newline.

Ordering rules:

- Sort by count, highest first.
- Break ties between words with equal counts alphabetically (ascending).
- If the file has fewer than `N` distinct words, print all of them.
- If `N` is `0`, print nothing.

## Example

A sample file `sample.txt` is included. Running:

```
python3 wordcount.py sample.txt 3
```

should print:

```
the 3
fox 2
quick 2
```

Done when `wordcount.py` behaves exactly as specified above.
