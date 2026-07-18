"""Renderer for the template engine.

Walks the AST produced by :mod:`template.parser` and evaluates it against a
context dict, returning the final string. All lookup, truthiness, filter, and
loop rules live here so the node classes can stay pure data.

Lookup semantics (:func:`resolve`):

* A ``path`` is split on ``.`` and each segment is resolved in turn.
* At each step, if the current object is a dict the segment is used as a key;
  otherwise it is looked up as an attribute.
* A segment that resolves to nothing yields the sentinel :data:`MISSING`, which
  renders as an empty string and is treated as falsy by ``{% if %}``.

Filters (see :data:`FILTERS`) are applied left to right to a resolved value
before it is stringified. Loops (:class:`~template.nodes.ForNode`) bind the loop
variable in a shallow copy of the context so the body sees it without mutating
the caller's dict.
"""

from .nodes import TextNode, VarNode, IfNode, ForNode


# Sentinel for "this lookup found nothing". Distinct from ``None`` so a context
# value that is literally ``None`` is not confused with an absent key.
MISSING = object()


class RenderError(Exception):
    """Raised on an unknown node, an unknown filter, or a bad filter argument."""


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
    """Convert a resolved (post-filter) value to its output string.

    ``MISSING`` and ``None`` both render as the empty string; everything else
    goes through ``str()``.
    """
    if value is MISSING or value is None:
        return ""
    return str(value)


# --- Filters ---------------------------------------------------------------
#
# Each filter takes ``(value, arg)`` and returns a new value. ``arg`` is the
# (unquoted) string argument, or ``None`` when the filter was written with no
# argument. Filters must tolerate a ``MISSING`` input (an absent lookup).


def _filter_upper(value, arg):
    return stringify(value).upper()


def _filter_lower(value, arg):
    return stringify(value).lower()


def _filter_length(value, arg):
    if value is MISSING or value is None:
        return 0
    try:
        return len(value)
    except TypeError:
        return len(stringify(value))


def _filter_default(value, arg):
    if arg is None:
        raise RenderError("default filter requires an argument")
    if value is MISSING or value is None or value == "":
        return arg
    return value


def _filter_join(value, arg):
    if arg is None:
        raise RenderError("join filter requires an argument")
    if value is MISSING or value is None:
        return ""
    return arg.join(stringify(item) for item in value)


FILTERS = {
    "upper": _filter_upper,
    "lower": _filter_lower,
    "length": _filter_length,
    "default": _filter_default,
    "join": _filter_join,
}


def apply_filters(value, filters):
    """Apply a ``[(name, arg), ...]`` pipeline to ``value`` left to right."""
    for name, arg in filters:
        func = FILTERS.get(name)
        if func is None:
            raise RenderError("unknown filter: {}".format(name))
        value = func(value, arg)
    return value


# --- Node rendering --------------------------------------------------------


def render_node(node, context):
    """Render a single AST node to a string."""
    if isinstance(node, TextNode):
        return node.text

    if isinstance(node, VarNode):
        value = resolve(node.path, context)
        value = apply_filters(value, node.filters)
        return stringify(value)

    if isinstance(node, IfNode):
        if is_truthy(resolve(node.path, context)):
            return render_nodes(node.body, context)
        return render_nodes(node.orelse, context)

    if isinstance(node, ForNode):
        iterable = resolve(node.path, context)
        if iterable is MISSING or iterable is None:
            return ""
        parts = []
        for item in iterable:
            scope = dict(context)
            scope[node.var] = item
            parts.append(render_nodes(node.body, scope))
        return "".join(parts)

    raise RenderError("cannot render node: {!r}".format(node))


def render_nodes(nodes, context):
    """Render a list of nodes and concatenate the results."""
    return "".join(render_node(node, context) for node in nodes)
