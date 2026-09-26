"""A small text template engine.

Public entry point:

    from template import render
    render("Hello, {{ name }}!", {"name": "world"})  # -> "Hello, world!"

The pipeline is three stages:

    lexer.tokenize   ->  parser.parse   ->  renderer.render_nodes

``render`` glues them together: it parses the template string into an AST and
renders that AST against the supplied context dict.
"""

from .parser import parse
from .renderer import render_nodes

__all__ = ["render"]


def render(template_str, context=None):
    """Render ``template_str`` against ``context`` and return the result string.

    ``context`` defaults to an empty dict, so a template with no variables can be
    rendered with a single argument.
    """
    if context is None:
        context = {}
    return render_nodes(parse(template_str), context)
