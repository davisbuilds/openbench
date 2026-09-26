"""webcore.templating -- a very small text template engine.

This is a deliberately tiny, original renderer -- enough for error pages and
simple HTML fragments, not a general-purpose engine. It understands three
construct families:

*   ``{{ expression }}`` -- interpolate a value (HTML-escaped by default).
*   ``{% if expr %} ... {% elif expr %} ... {% else %} ... {% endif %}``
*   ``{% for item in iterable %} ... {% else %} ... {% endfor %}``

plus ``{# a comment #}`` and ``{{ value | filter }}`` pipes. Expressions are a
restricted mini-language (names, attribute/index access, literals, comparisons,
``and``/``or``/``not``, and ``|`` filters) evaluated against a context ``dict`` --
it is **not** ``eval`` and cannot reach arbitrary Python.

Rendering pipeline: :class:`Lexer` -> tokens, :class:`Parser` -> a node tree,
node ``render`` -> text. A :class:`Template` wraps the whole thing; an
:class:`Environment` holds shared filters/globals and caches compiled templates.

Example
-------
::

    env = Environment()
    tmpl = env.from_string("<h1>{{ title | upper }}</h1>{% for u in users %}"
                           "<li>{{ u.name }}</li>{% endfor %}")
    tmpl.render(title="hi", users=[{"name": "sam"}])
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "TemplateError",
    "TemplateSyntaxError",
    "Environment",
    "Template",
    "escape",
]


class TemplateError(Exception):
    """Base class for every templating error."""


class TemplateSyntaxError(TemplateError):
    """A malformed tag, unbalanced block, or unparsable expression."""


def escape(value: Any) -> str:
    """HTML-escape ``value`` (``&<>"'`` -> entities); ``None`` becomes ``""``."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


class Markup(str):
    """A string already known to be safe HTML, exempt from auto-escaping."""

    def __html__(self) -> str:
        return str(self)


# --------------------------------------------------------------------------
# Lexing
# --------------------------------------------------------------------------

#: Matches any of the four delimiter kinds so we can split raw source.
_TOKEN_RE = re.compile(
    r"(?P<comment>\{#.*?#\})"
    r"|(?P<var>\{\{.*?\}\})"
    r"|(?P<block>\{%.*?%\})",
    re.DOTALL,
)


class Token:
    """A lexed chunk: ``kind`` in ``text|var|block``, plus its inner ``value``."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str) -> None:
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:
        return "Token({!r}, {!r})".format(self.kind, self.value)


class Lexer:
    """Split template source into literal-text, ``{{...}}`` and ``{%...%}`` tokens."""

    def tokenize(self, source: str) -> List[Token]:
        tokens: List[Token] = []
        pos = 0
        for match in _TOKEN_RE.finditer(source):
            start = match.start()
            if start > pos:
                tokens.append(Token("text", source[pos:start]))
            if match.lastgroup == "comment":
                pass  # comments are dropped entirely
            elif match.lastgroup == "var":
                tokens.append(Token("var", match.group()[2:-2].strip()))
            else:  # block
                tokens.append(Token("block", match.group()[2:-2].strip()))
            pos = match.end()
        if pos < len(source):
            tokens.append(Token("text", source[pos:]))
        return tokens


# --------------------------------------------------------------------------
# Expression evaluation
# --------------------------------------------------------------------------

_STRING_RE = re.compile(r"""^(['"])(.*)\1$""", re.DOTALL)
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]']*$")
_COMPARISONS: Dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class ExpressionEvaluator:
    """Evaluate the restricted expression grammar against a context.

    Supports: literals (numbers, quoted strings, ``true``/``false``/``none``),
    dotted/indexed name lookups (``user.name``, ``items[0]``), ``not``/``and``/
    ``or``, the six comparisons, and trailing ``| filter`` pipes. Anything else
    raises :class:`TemplateSyntaxError`. Filters come from the environment.
    """

    def __init__(self, filters: Dict[str, Callable[..., Any]]) -> None:
        self.filters = filters

    def evaluate(self, expr: str, context: Dict[str, Any]) -> Any:
        expr = expr.strip()
        if not expr:
            return ""
        # Filters bind loosest: split on top-level '|'.
        head, pipes = self._split_pipes(expr)
        value = self._eval_or(head, context)
        for pipe in pipes:
            value = self._apply_filter(pipe, value, context)
        return value

    # -- filter pipes ----------------------------------------------------

    def _split_pipes(self, expr: str) -> Tuple[str, List[str]]:
        parts = self._split_top_level(expr, "|")
        return parts[0], parts[1:]

    def _apply_filter(self, pipe: str, value: Any, context: Dict[str, Any]) -> Any:
        pipe = pipe.strip()
        name, _, arg_src = pipe.partition(":")
        name = name.strip()
        func = self.filters.get(name)
        if func is None:
            raise TemplateSyntaxError("unknown filter {!r}".format(name))
        if arg_src.strip():
            arg = self._eval_atom(arg_src.strip(), context)
            return func(value, arg)
        return func(value)

    # -- boolean / comparison layers ------------------------------------

    def _eval_or(self, expr: str, context: Dict[str, Any]) -> Any:
        parts = self._split_top_level(expr, " or ")
        if len(parts) > 1:
            return any(self._truthy(self._eval_and(p, context)) for p in parts)
        return self._eval_and(expr, context)

    def _eval_and(self, expr: str, context: Dict[str, Any]) -> Any:
        parts = self._split_top_level(expr, " and ")
        if len(parts) > 1:
            return all(self._truthy(self._eval_not(p, context)) for p in parts)
        return self._eval_not(expr, context)

    def _eval_not(self, expr: str, context: Dict[str, Any]) -> Any:
        expr = expr.strip()
        if expr.startswith("not "):
            return not self._truthy(self._eval_comparison(expr[4:], context))
        return self._eval_comparison(expr, context)

    def _eval_comparison(self, expr: str, context: Dict[str, Any]) -> Any:
        for op in ("==", "!=", "<=", ">=", "<", ">"):
            parts = self._split_top_level(expr, op)
            if len(parts) == 2:
                left = self._eval_atom(parts[0].strip(), context)
                right = self._eval_atom(parts[1].strip(), context)
                return _COMPARISONS[op](left, right)
        return self._eval_atom(expr.strip(), context)

    # -- atoms -----------------------------------------------------------

    def _eval_atom(self, expr: str, context: Dict[str, Any]) -> Any:
        expr = expr.strip()
        if expr.startswith("(") and expr.endswith(")"):
            return self._eval_or(expr[1:-1], context)
        low = expr.lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("none", "null"):
            return None
        string_match = _STRING_RE.match(expr)
        if string_match:
            return string_match.group(2)
        if _NUMBER_RE.match(expr):
            return float(expr) if "." in expr else int(expr)
        if _NAME_RE.match(expr):
            return self._resolve_name(expr, context)
        raise TemplateSyntaxError("cannot evaluate {!r}".format(expr))

    def _resolve_name(self, name: str, context: Dict[str, Any]) -> Any:
        # Split "a.b[0].c" into ["a", "b", "0", "c"].
        tokens = re.split(r"\.|\[|\]", name)
        tokens = [t.strip("'\"") for t in tokens if t != ""]
        if not tokens:
            return None
        current: Any = context.get(tokens[0], "")
        for token in tokens[1:]:
            current = self._get_member(current, token)
            if current == "" or current is None:
                # keep walking only while something exists
                if current is None:
                    return None
        return current

    @staticmethod
    def _get_member(obj: Any, token: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            if token in obj:
                return obj[token]
            return ""
        if isinstance(obj, (list, tuple)):
            try:
                return obj[int(token)]
            except (ValueError, IndexError):
                return ""
        return getattr(obj, token, "")

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _truthy(value: Any) -> bool:
        return bool(value)

    @staticmethod
    def _split_top_level(expr: str, sep: str) -> List[str]:
        """Split ``expr`` on ``sep`` ignoring occurrences inside quotes/parens."""
        parts: List[str] = []
        depth = 0
        quote: Optional[str] = None
        i = 0
        buf: List[str] = []
        seplen = len(sep)
        while i < len(expr):
            ch = expr[i]
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in "'\"":
                quote = ch
                buf.append(ch)
                i += 1
                continue
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            if depth == 0 and expr[i:i + seplen] == sep:
                parts.append("".join(buf))
                buf = []
                i += seplen
                continue
            buf.append(ch)
            i += 1
        parts.append("".join(buf))
        return parts


# --------------------------------------------------------------------------
# Parse tree
# --------------------------------------------------------------------------

class Node:
    """Base node in a compiled template tree."""

    def render(self, context: Dict[str, Any], engine: "Template") -> str:
        raise NotImplementedError


class TextNode(Node):
    """A literal run of template text, emitted verbatim."""

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self, context: Dict[str, Any], engine: "Template") -> str:
        return self.text


class VarNode(Node):
    """A ``{{ expr }}`` interpolation, auto-escaped unless the value is Markup."""

    def __init__(self, expr: str) -> None:
        self.expr = expr

    def render(self, context: Dict[str, Any], engine: "Template") -> str:
        value = engine.evaluator.evaluate(self.expr, context)
        if isinstance(value, Markup) or hasattr(value, "__html__"):
            return str(value)
        if engine.autoescape:
            return escape(value)
        return "" if value is None else str(value)


class IfNode(Node):
    """An ``if/elif/else`` chain of ``(expr, body)`` branches."""

    def __init__(self, branches: List[Tuple[Optional[str], List[Node]]]) -> None:
        self.branches = branches

    def render(self, context: Dict[str, Any], engine: "Template") -> str:
        for expr, body in self.branches:
            if expr is None or engine.evaluator._truthy(
                engine.evaluator.evaluate(expr, context)
            ):
                return _render_nodes(body, context, engine)
        return ""


class ForNode(Node):
    """A ``{% for x in seq %}`` loop with an optional empty ``{% else %}``."""

    def __init__(self, var: str, iterable_expr: str, body: List[Node],
                 empty: List[Node]) -> None:
        self.var = var
        self.iterable_expr = iterable_expr
        self.body = body
        self.empty = empty

    def render(self, context: Dict[str, Any], engine: "Template") -> str:
        iterable = engine.evaluator.evaluate(self.iterable_expr, context)
        if not iterable:
            return _render_nodes(self.empty, context, engine)
        items = list(iterable)
        out: List[str] = []
        for index, item in enumerate(items):
            loop_ctx = dict(context)
            loop_ctx[self.var] = item
            loop_ctx["loop"] = {
                "index": index + 1,
                "index0": index,
                "first": index == 0,
                "last": index == len(items) - 1,
                "length": len(items),
            }
            out.append(_render_nodes(self.body, loop_ctx, engine))
        return "".join(out)


def _render_nodes(nodes: Sequence[Node], context: Dict[str, Any],
                  engine: "Template") -> str:
    return "".join(node.render(context, engine) for node in nodes)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class Parser:
    """Turn a flat token list into a node tree, validating block nesting."""

    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> List[Node]:
        nodes, terminator = self._parse_until(())
        if terminator is not None:
            raise TemplateSyntaxError("unexpected {{% {} %}}".format(terminator))
        return nodes

    def _parse_until(self, stops: Tuple[str, ...]) -> Tuple[List[Node], Optional[str]]:
        nodes: List[Node] = []
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            if token.kind == "text":
                nodes.append(TextNode(token.value))
                self.pos += 1
            elif token.kind == "var":
                nodes.append(VarNode(token.value))
                self.pos += 1
            else:  # block
                keyword = token.value.split(None, 1)[0]
                if keyword in stops:
                    return nodes, keyword
                self.pos += 1
                nodes.append(self._parse_block(token.value, keyword))
        return nodes, None

    def _parse_block(self, source: str, keyword: str) -> Node:
        if keyword == "if":
            return self._parse_if(source)
        if keyword == "for":
            return self._parse_for(source)
        raise TemplateSyntaxError("unknown block tag {!r}".format(keyword))

    def _parse_if(self, source: str) -> Node:
        branches: List[Tuple[Optional[str], List[Node]]] = []
        condition = source[len("if"):].strip()
        while True:
            body, terminator = self._parse_until(("elif", "else", "endif"))
            branches.append((condition, body))
            if terminator == "endif" or terminator is None:
                self._consume_block()
                break
            block = self.tokens[self.pos].value
            self.pos += 1
            if terminator == "elif":
                condition = block[len("elif"):].strip()
            else:  # else
                condition = None
        return IfNode(branches)

    def _parse_for(self, source: str) -> Node:
        rest = source[len("for"):].strip()
        if " in " not in rest:
            raise TemplateSyntaxError("malformed for tag: {!r}".format(source))
        var, _, iterable_expr = rest.partition(" in ")
        var = var.strip()
        body, terminator = self._parse_until(("else", "endfor"))
        empty: List[Node] = []
        if terminator == "else":
            self.pos += 1
            empty, _ = self._parse_until(("endfor",))
        self._consume_block()
        return ForNode(var, iterable_expr.strip(), body, empty)

    def _consume_block(self) -> None:
        # Skip the closing block tag we stopped on (endif / endfor).
        if self.pos < len(self.tokens) and self.tokens[self.pos].kind == "block":
            self.pos += 1


# --------------------------------------------------------------------------
# Public template / environment
# --------------------------------------------------------------------------

class Template:
    """A compiled template ready to :meth:`render` against a context."""

    def __init__(self, source: str, environment: Optional["Environment"] = None) -> None:
        self.source = source
        self.environment = environment or Environment()
        self.autoescape = self.environment.autoescape
        self.evaluator = ExpressionEvaluator(self.environment.filters)
        tokens = Lexer().tokenize(source)
        self.nodes = Parser(tokens).parse()

    def render(self, **context: Any) -> str:
        """Render with keyword context values, layered over environment globals."""
        return self.render_context(context)

    def render_context(self, context: Dict[str, Any]) -> str:
        """Render with an explicit context ``dict`` (globals applied underneath)."""
        merged = dict(self.environment.globals)
        merged.update(context)
        return _render_nodes(self.nodes, merged, self)

    def __repr__(self) -> str:
        preview = self.source[:24].replace("\n", " ")
        return "<Template {!r}...>".format(preview)


#: The filters every :class:`Environment` starts with.
def _default_filters() -> Dict[str, Callable[..., Any]]:
    return {
        "upper": lambda v: str(v).upper(),
        "lower": lambda v: str(v).lower(),
        "title": lambda v: str(v).title(),
        "capitalize": lambda v: str(v).capitalize(),
        "strip": lambda v: str(v).strip(),
        "length": lambda v: len(v),
        "default": lambda v, d="": v if v not in ("", None) else d,
        "escape": lambda v: escape(v),
        "safe": lambda v: Markup(v if v is not None else ""),
        "join": lambda v, sep=", ": sep.join(str(x) for x in v),
        "reverse": lambda v: v[::-1] if isinstance(v, str) else list(reversed(v)),
        "first": lambda v: v[0] if v else "",
        "last": lambda v: v[-1] if v else "",
        "trim": lambda v: str(v).strip(),
        "replace": lambda v, arg: str(v).replace(arg, ""),
    }


class Environment:
    """Shared configuration for a group of templates.

    Holds the :attr:`filters` available to expressions, :attr:`globals` merged
    into every render, an :attr:`autoescape` flag, and a small compile cache so
    the same source string is only parsed once.
    """

    def __init__(self, autoescape: bool = True,
                 filters: Optional[Dict[str, Callable[..., Any]]] = None,
                 globals: Optional[Dict[str, Any]] = None) -> None:
        self.autoescape = autoescape
        self.filters = _default_filters()
        if filters:
            self.filters.update(filters)
        self.globals: Dict[str, Any] = dict(globals or {})
        self._cache: Dict[str, Template] = {}

    def add_filter(self, name: str, func: Callable[..., Any]) -> None:
        """Register (or replace) a filter usable as ``{{ v | name }}``."""
        self.filters[name] = func

    def from_string(self, source: str) -> Template:
        """Compile ``source`` into a :class:`Template`, caching by exact text."""
        cached = self._cache.get(source)
        if cached is None:
            cached = Template(source, self)
            self._cache[source] = cached
        return cached

    def render_string(self, source: str, **context: Any) -> str:
        """One-shot compile-and-render convenience."""
        return self.from_string(source).render(**context)

    def __repr__(self) -> str:
        return "<Environment autoescape={} filters={}>".format(
            self.autoescape, len(self.filters)
        )
