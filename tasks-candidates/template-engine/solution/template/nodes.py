"""AST node types for the template engine.

The parser turns a flat token stream into a tree of these nodes; the renderer
walks that tree against a context dict. Nodes are plain data holders -- they
carry no rendering logic themselves so that all evaluation lives in one place
(:mod:`template.renderer`).

Node kinds currently defined:

* :class:`TextNode` -- a run of literal text, emitted verbatim.
* :class:`VarNode`  -- a ``{{ path | filter:... }}`` substitution; ``path`` is a
  dotted lookup key and ``filters`` is the ordered pipeline applied to it.
* :class:`IfNode`   -- a ``{% if path %}...{% else %}...{% endif %}`` block; the
  ``else`` branch may be empty.
* :class:`ForNode`  -- a ``{% for var in path %}...{% endfor %}`` loop.
"""


class TextNode:
    """Literal text copied straight through to the output."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "TextNode({!r})".format(self.text)


class VarNode:
    """A ``{{ path | filter | filter:"arg" }}`` variable substitution.

    ``path`` is a dotted lookup string (e.g. ``"user.name"``) resolved against
    the render context. ``filters`` is a list of ``(name, arg)`` tuples applied
    left to right to the resolved value; ``arg`` is the (unquoted) string
    argument or ``None`` when the filter takes no argument.
    """

    __slots__ = ("path", "filters")

    def __init__(self, path, filters=None):
        self.path = path
        self.filters = filters if filters is not None else []

    def __repr__(self):
        return "VarNode({!r}, {!r})".format(self.path, self.filters)


class IfNode:
    """A conditional block.

    ``path`` is the dotted lookup whose truthiness selects a branch. ``body`` is
    the list of nodes rendered when the condition is truthy; ``orelse`` is the
    list rendered otherwise (empty when there is no ``{% else %}``).
    """

    __slots__ = ("path", "body", "orelse")

    def __init__(self, path, body, orelse):
        self.path = path
        self.body = body
        self.orelse = orelse

    def __repr__(self):
        return "IfNode({!r}, {!r}, {!r})".format(self.path, self.body, self.orelse)


class ForNode:
    """A loop block: ``{% for var in path %} body {% endfor %}``.

    ``var`` is the loop variable name bound in the body scope for each item.
    ``path`` is the dotted lookup for the iterable. ``body`` is the list of nodes
    rendered once per item.
    """

    __slots__ = ("var", "path", "body")

    def __init__(self, var, path, body):
        self.var = var
        self.path = path
        self.body = body

    def __repr__(self):
        return "ForNode({!r}, {!r}, {!r})".format(self.var, self.path, self.body)
