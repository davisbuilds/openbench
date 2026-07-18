"""Parser for the template engine.

Consumes the flat token list from :mod:`template.lexer` and produces a tree of
:mod:`template.nodes`. The parser is a small recursive-descent walker: it reads
tokens left to right, turns ``TEXT`` / ``VAR`` tokens into leaf nodes, and
recurses into block bodies when it meets a ``{% if %}`` opener, stopping when it
reaches the matching ``{% else %}`` / ``{% endif %}``.

Only the ``if`` block tag is understood right now. Any other block keyword is a
:class:`ParseError`.
"""

from .lexer import tokenize, TEXT, VAR, BLOCK
from .nodes import TextNode, VarNode, IfNode


class ParseError(Exception):
    """Raised when the token stream does not form a valid template tree."""


def _keyword(block_value):
    """Return the first word of a block tag (``"if x"`` -> ``"if"``)."""
    if not block_value:
        return ""
    return block_value.split(None, 1)[0]


class Parser:
    """Turns a token list into a list of AST nodes."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def parse(self):
        """Parse the whole token stream into a list of top-level nodes."""
        nodes, stop = self._parse_nodes(stop_tags=())
        if stop is not None:
            # A closing tag with no matching opener bubbled up to the top.
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
                    # Leave the stop tag unconsumed for the caller to handle.
                    return nodes, keyword
                if keyword == "if":
                    nodes.append(self._parse_if(tok.value))
                else:
                    raise ParseError("unknown block tag {!r}".format(tok.value))
            else:  # pragma: no cover - lexer only emits the three known types
                raise ParseError("unknown token type {!r}".format(tok.type))

        return nodes, None

    def _parse_var(self, expr):
        """Build a :class:`VarNode` from a ``{{ ... }}`` expression."""
        path = expr.strip()
        if not path:
            raise ParseError("empty variable tag {{{{ }}}}")
        return VarNode(path)

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


def parse(template):
    """Convenience wrapper: tokenize ``template`` and parse it to a node list."""
    return Parser(tokenize(template)).parse()
