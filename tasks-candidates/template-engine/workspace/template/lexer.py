"""Lexer for the template engine.

The lexer scans a raw template string once, left to right, and breaks it into a
flat list of :class:`Token` objects. It understands exactly three surface
constructs:

* literal text          -> ``TEXT`` token (its ``value`` is the raw run of text)
* ``{{ expression }}``  -> ``VAR`` token  (``value`` is the trimmed inner text)
* ``{% tag %}``         -> ``BLOCK`` token (``value`` is the trimmed inner text)

The lexer is intentionally dumb: it does not know what a ``VAR`` expression or a
``BLOCK`` tag *means*. It only splits the stream so the parser can impose
structure. Everything that is not a ``{{`` / ``{%`` opener is accumulated as
literal text until the next opener (or the end of the template).
"""

# Token type constants. Kept as plain strings so tokens are easy to print and
# compare in tests and in the parser.
TEXT = "TEXT"
VAR = "VAR"
BLOCK = "BLOCK"

# The delimiter pairs the lexer recognises, mapped opener -> (closer, type).
_DELIMITERS = {
    "{{": ("}}", VAR),
    "{%": ("%}", BLOCK),
}


class LexError(Exception):
    """Raised when the template contains a malformed / unterminated tag."""


class Token:
    """A single lexical unit produced by :func:`tokenize`.

    ``type`` is one of :data:`TEXT`, :data:`VAR`, :data:`BLOCK`. ``value`` is the
    raw text for a ``TEXT`` token, or the whitespace-trimmed inner content for a
    ``VAR`` / ``BLOCK`` token (the surrounding delimiters are stripped).
    """

    __slots__ = ("type", "value")

    def __init__(self, type, value):
        self.type = type
        self.value = value

    def __repr__(self):
        return "Token({!r}, {!r})".format(self.type, self.value)

    def __eq__(self, other):
        return (
            isinstance(other, Token)
            and self.type == other.type
            and self.value == other.value
        )


def tokenize(template):
    """Split ``template`` into a list of :class:`Token` objects.

    Raises :class:`LexError` if a ``{{`` or ``{%`` is opened but never closed.
    """
    tokens = []
    i = 0
    n = len(template)
    text_start = 0

    while i < n:
        opener = template[i:i + 2]
        if opener in _DELIMITERS:
            # Flush any literal text accumulated before this opener.
            if i > text_start:
                tokens.append(Token(TEXT, template[text_start:i]))

            closer, ttype = _DELIMITERS[opener]
            end = template.find(closer, i + 2)
            if end == -1:
                raise LexError(
                    "unterminated {!r} tag starting at index {}".format(opener, i)
                )
            inner = template[i + 2:end].strip()
            tokens.append(Token(ttype, inner))

            i = end + len(closer)
            text_start = i
        else:
            i += 1

    # Trailing literal text after the last tag (or the whole string if no tags).
    if n > text_start:
        tokens.append(Token(TEXT, template[text_start:n]))

    return tokens
