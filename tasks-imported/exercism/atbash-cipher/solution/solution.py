"""Reference solution: Atbash cipher."""


def _mirror(ch):
    if ch.isalpha():
        return chr(ord("z") - (ord(ch) - ord("a")))
    return ch  # a digit passes through unchanged


def _transform(phrase):
    return "".join(_mirror(c) for c in phrase.lower() if c.isalnum())


def encode(phrase):
    text = _transform(phrase)
    return " ".join(text[i:i + 5] for i in range(0, len(text), 5))


def decode(phrase):
    return _transform(phrase)
