"""AST node types for the template engine.

The parser turns a flat token stream into a tree of these nodes; the renderer
walks that tree against a context dict. Nodes are plain data holders -- they
carry no rendering logic themselves so that all evaluation lives in one place
(:mod:`template.renderer`).

Node kinds currently defined:

* :class:`TextNode` -- a run of literal text, emitted verbatim.
* :class:`VarNode`  -- a ``{{ ... }}`` substitution; ``path`` is a dotted lookup
  key resolved against the context.
* :class:`IfNode`   -- a ``{% if path %}...{% else %}...{% endif %}`` block; the
  ``else`` branch may be empty.
"""


class TextNode:
    """Literal text copied straight through to the output."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "TextNode({!r})".format(self.text)


class VarNode:
    """A ``{{ path }}`` variable substitution.

    ``path`` is a dotted lookup string (e.g. ``"user.name"``) resolved against
    the render context.
    """

    __slots__ = ("path",)

    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return "VarNode({!r})".format(self.path)


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
