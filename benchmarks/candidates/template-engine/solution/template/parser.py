"""Parser for the template engine.

Consumes the flat token list from :mod:`template.lexer` and produces a tree of
:mod:`template.nodes`. The parser is a small recursive-descent walker: it reads
tokens left to right, turns ``TEXT`` / ``VAR`` tokens into leaf nodes, and
recurses into block bodies when it meets an ``{% if %}`` or ``{% for %}`` opener,
stopping when it reaches the matching close tag.

Block tags understood: ``if`` (with optional ``else``) and ``for``. Any other
block keyword is a :class:`ParseError`.

``VAR`` expressions may carry a filter pipeline: ``{{ path | f1 | f2:"arg" }}``.
The path/filter split is parsed here into a :class:`~template.nodes.VarNode`.
"""

from .lexer import tokenize, TEXT, VAR, BLOCK
from .nodes import TextNode, VarNode, IfNode, ForNode


class ParseError(Exception):
    """Raised when the token stream does not form a valid template tree."""


def _keyword(block_value):
    """Return the first word of a block tag (``"if x"`` -> ``"if"``)."""
    if not block_value:
        return ""
    return block_value.split(None, 1)[0]


def _split_pipeline(expr):
    """Split a var expression on top-level ``|``, respecting double quotes.

    ``'tags | join:"|" | upper'`` -> ``['tags ', ' join:"|" ', ' upper']``.
    A ``|`` inside a double-quoted string is not treated as a separator.
    """
    parts = []
    buf = []
    in_quote = False
    for ch in expr:
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == "|" and not in_quote:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_arg(raw):
    """Parse a filter argument, which must be a double-quoted string literal."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    raise ParseError("filter argument must be a quoted string: {!r}".format(raw))


def _parse_filter(segment):
    """Parse one pipeline segment into ``(name, arg)``.

    ``'upper'`` -> ``('upper', None)``; ``'default:"n/a"'`` -> ``('default', 'n/a')``.
    """
    segment = segment.strip()
    if ":" in segment:
        name, _, raw = segment.partition(":")
        name = name.strip()
        arg = _parse_arg(raw)
    else:
        name = segment
        arg = None
    if not name:
        raise ParseError("filter name is empty in {!r}".format(segment))
    return name, arg


def parse_var_expression(expr):
    """Parse a ``{{ ... }}`` inner expression into ``(path, filters)``."""
    segments = _split_pipeline(expr)
    path = segments[0].strip()
    if not path:
        raise ParseError("empty variable tag {{{{ }}}}")
    filters = []
    for segment in segments[1:]:
        if not segment.strip():
            raise ParseError("empty filter in {!r}".format(expr))
        filters.append(_parse_filter(segment))
    return path, filters


class Parser:
    """Turns a token list into a list of AST nodes."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def parse(self):
        """Parse the whole token stream into a list of top-level nodes."""
        nodes, stop = self._parse_nodes(stop_tags=())
        if stop is not None:
            raise ParseError("unexpected {{% {} %}}".format(stop))
        return nodes

    def _parse_nodes(self, stop_tags):
        """Parse nodes until end-of-stream or a block keyword in ``stop_tags``.

        Returns ``(nodes, stop_keyword)`` where ``stop_keyword`` is the block
        keyword that halted parsing (still unconsumed), or ``None`` at EOF.
        """
        nodes = []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]

            if tok.type == TEXT:
                nodes.append(TextNode(tok.value))
                self.pos += 1
            elif tok.type == VAR:
                nodes.append(self._parse_var(tok.value))
                self.pos += 1
            elif tok.type == BLOCK:
                keyword = _keyword(tok.value)
                if keyword in stop_tags:
                    return nodes, keyword
                if keyword == "if":
                    nodes.append(self._parse_if(tok.value))
                elif keyword == "for":
                    nodes.append(self._parse_for(tok.value))
                else:
                    raise ParseError("unknown block tag {!r}".format(tok.value))
            else:  # pragma: no cover - lexer only emits the three known types
                raise ParseError("unknown token type {!r}".format(tok.type))

        return nodes, None

    def _parse_var(self, expr):
        """Build a :class:`VarNode` (with its filter pipeline) from an expression."""
        path, filters = parse_var_expression(expr)
        return VarNode(path, filters)

    def _parse_if(self, directive):
        """Parse ``{% if cond %} body [ {% else %} body ] {% endif %}``.

        Called with ``self.pos`` pointing at the ``{% if %}`` token.
        """
        parts = directive.split(None, 1)
        if len(parts) != 2 or not parts[1].strip():
            raise ParseError("if tag requires a condition: {!r}".format(directive))
        cond_path = parts[1].strip()

        self.pos += 1  # consume the {% if %} token
        body, stop = self._parse_nodes(stop_tags=("else", "endif"))

        orelse = []
        if stop == "else":
            self.pos += 1  # consume the {% else %} token
            orelse, stop = self._parse_nodes(stop_tags=("endif",))

        if stop != "endif":
            raise ParseError("if block is missing its {% endif %}")
        self.pos += 1  # consume the {% endif %} token

        return IfNode(cond_path, body, orelse)

    def _parse_for(self, directive):
        """Parse ``{% for var in path %} body {% endfor %}``.

        Called with ``self.pos`` pointing at the ``{% for %}`` token.
        """
        parts = directive.split()
        if len(parts) != 4 or parts[0] != "for" or parts[2] != "in":
            raise ParseError(
                "for tag must read 'for <var> in <path>': {!r}".format(directive)
            )
        var_name = parts[1]
        iterable_path = parts[3]

        self.pos += 1  # consume the {% for %} token
        body, stop = self._parse_nodes(stop_tags=("endfor",))

        if stop != "endfor":
            raise ParseError("for block is missing its {% endfor %}")
        self.pos += 1  # consume the {% endfor %} token

        return ForNode(var_name, iterable_path, body)


def parse(template):
    """Convenience wrapper: tokenize ``template`` and parse it to a node list."""
    return Parser(tokenize(template)).parse()
