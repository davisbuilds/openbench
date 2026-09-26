"""Renderer for the template engine.

Walks the AST produced by :mod:`template.parser` and evaluates it against a
context dict, returning the final string. All lookup and truthiness rules live
here so the node classes can stay pure data.

Lookup semantics (:func:`resolve`):

* A ``path`` is split on ``.`` and each segment is resolved in turn.
* At each step, if the current object is a dict the segment is used as a key;
  otherwise it is looked up as an attribute.
* A segment that resolves to nothing yields the sentinel :data:`MISSING`, which
  renders as an empty string and is treated as falsy by ``{% if %}``.
"""

from .nodes import TextNode, VarNode, IfNode


# Sentinel for "this lookup found nothing". Distinct from ``None`` so a context
# value that is literally ``None`` is not confused with an absent key.
MISSING = object()


class RenderError(Exception):
    """Raised when the AST contains a node the renderer does not understand."""


def resolve(path, context):
    """Resolve a dotted ``path`` against ``context``.

    Returns the looked-up value, or :data:`MISSING` if any segment is absent.
    """
    current = context
    for segment in path.split("."):
        if current is MISSING:
            return MISSING
        if isinstance(current, dict):
            if segment in current:
                current = current[segment]
            else:
                return MISSING
        elif hasattr(current, segment):
            current = getattr(current, segment)
        else:
            return MISSING
    return current


def is_truthy(value):
    """Truthiness test used by ``{% if %}``. MISSING is always falsy."""
    if value is MISSING:
        return False
    return bool(value)


def stringify(value):
    """Convert a resolved value to its output string.

    ``MISSING`` and ``None`` both render as the empty string; everything else
    goes through ``str()``.
    """
    if value is MISSING or value is None:
        return ""
    return str(value)


def render_node(node, context):
    """Render a single AST node to a string."""
    if isinstance(node, TextNode):
        return node.text

    if isinstance(node, VarNode):
        return stringify(resolve(node.path, context))

    if isinstance(node, IfNode):
        if is_truthy(resolve(node.path, context)):
            return render_nodes(node.body, context)
        return render_nodes(node.orelse, context)

    raise RenderError("cannot render node: {!r}".format(node))


def render_nodes(nodes, context):
    """Render a list of nodes and concatenate the results."""
    return "".join(render_node(node, context) for node in nodes)
