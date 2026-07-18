# template

A small, dependency-free text template engine (Python 3 standard library only).

## Entry point

```python
from template import render

render(template_str, context)
```

`template.render(template_str, context)` takes a template string and a context
dict and returns the rendered string. `context` defaults to `{}`.

```python
from template import render

render("Hello, {{ user.name }}!", {"user": {"name": "Ada"}})
# -> "Hello, Ada!"
```

## Source layout

The engine is a three-stage pipeline; each stage is one module under
`template/`:

| File               | Responsibility                                              |
|--------------------|-------------------------------------------------------------|
| `template/lexer.py`    | Scan the template string into `TEXT` / `VAR` / `BLOCK` tokens. |
| `template/parser.py`   | Turn the token list into an AST of node objects.            |
| `template/nodes.py`    | The AST node classes (plain data holders).                  |
| `template/renderer.py` | Walk the AST against the context dict and produce the string. |
| `template/__init__.py` | Exposes the top-level `render(template_str, context)`.      |

## Currently supported syntax

- **Literal text** — copied through verbatim.
- **Variables** — `{{ name }}`, with dotted lookup `{{ user.name }}` that works
  over dicts (by key) and objects (by attribute). A missing name renders as the
  empty string.
- **Conditionals** — `{% if cond %}...{% else %}...{% endif %}`, branching on the
  truthiness of a (possibly dotted) context value. The `{% else %}` is optional
  and `if` blocks may be nested.
