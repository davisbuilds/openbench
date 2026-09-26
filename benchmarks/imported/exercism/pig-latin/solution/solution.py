"""Reference solution: Pig Latin."""

VOWELS = "aeiou"


def _word(word):
    if word[0] in VOWELS or word[:2] in ("xr", "yt"):
        return word + "ay"
    i = 0
    while i < len(word):
        if word[i] == "q" and i + 1 < len(word) and word[i + 1] == "u":
            i += 2
            break
        if word[i] == "y" and i > 0:
            break
        if word[i] in VOWELS:
            break
        i += 1
    return word[i:] + word[:i] + "ay"


def translate(phrase):
    return " ".join(_word(w) for w in phrase.split(" "))
