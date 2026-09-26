# Add loops and filters to the template engine

`template/` is a small, self-contained text template engine (Python 3 standard
library only). Its entry point is `template.render(template_str, context)`, which
parses a template string and renders it against a context dict. See
`README.md` for the source layout — the engine is a `lexer -> parser -> renderer`
pipeline with the AST node classes in `template/nodes.py`.

## What already works (must keep working)

- **Literal text** is emitted verbatim.
- **Variables**: `{{ name }}`, with **dotted lookup** `{{ user.name }}` resolving
  through dicts (by key) and objects (by attribute). A name that cannot be
  resolved renders as the empty string.
- **Conditionals**: `{% if cond %}...{% else %}...{% endif %}`, branching on the
  truthiness of a (possibly dotted) context value. `{% else %}` is optional, and
  `if` blocks may be nested.

## What to add

Add the two features below. They span the lexer, parser, node, and renderer
modules. Do not change the existing behavior above or the `render` signature.

### 1. For loops

Syntax: `{% for <var> in <path> %}...body...{% endfor %}`

- `<path>` is a dotted lookup (same rules as variables) that must resolve to an
  iterable. Iterate over it, and for each item bind `<var>` in the loop body's
  scope so the body can reference it (e.g. `{{ item }}` or, for dict/object
  items, `{{ item.name }}`).
- The loop variable is only visible inside the loop body; it must not leak into
  or overwrite the surrounding context after the loop.
- If the iterable is **empty or absent** (the path resolves to nothing), the loop
  renders nothing (its body is skipped entirely).
- Loops may be **nested**, and may contain `{% if %}` blocks; likewise an `if`
  block may contain a `for` loop. Blocks must nest correctly to their matching
  close tag.

Example:

```
{% for u in users %}{{ u.name }} {% endfor %}
```

with `{"users": [{"name": "Ann"}, {"name": "Bo"}]}` renders `Ann Bo `.

### 2. Filters on variable output

A variable tag may pipe its value through one or more **filters** using `|`:

```
{{ value | filter }}
{{ value | filter:"arg" }}
{{ value | filter1 | filter2:"arg" }}
```

- Filters are applied **left to right**: each filter receives the previous
  result. Chaining is arbitrary-length.
- A filter argument, when present, is written after a colon as a **double-quoted
  string literal** (e.g. `default:"n/a"`, `join:", "`). Only the filters that
  take an argument use one.
- Filters apply only to `{{ ... }}` variable output, not to `{% if %}` /
  `{% for %}` tag expressions.

Support **exactly** these filters:

| Filter        | Argument | Behavior                                                                                 |
|---------------|----------|------------------------------------------------------------------------------------------|
| `upper`       | none     | Uppercase the value's string form.                                                       |
| `lower`       | none     | Lowercase the value's string form.                                                       |
| `length`      | none     | The length (`len`) of the value — e.g. characters of a string, items of a list.          |
| `default:"x"` | required | If the value is **missing** (path not resolvable), `None`, or the **empty string**, render `x` instead; otherwise render the value unchanged. |
| `join:"s"`    | required | Join an iterable value into a string using the separator `s` between the items.          |

Examples:

- `{{ name | upper }}` with `{"name": "dune"}` -> `DUNE`
- `{{ items | length }}` with `{"items": [1, 2, 3]}` -> `3`
- `{{ nick | default:"n/a" }}` with `{}` -> `n/a`
- `{{ tags | join:", " }}` with `{"tags": ["a", "b", "c"]}` -> `a, b, c`
- `{{ tags | join:"-" | upper }}` with `{"tags": ["a", "b"]}` -> `A-B`

## Notes

- Keep it Python 3 standard library only — no third-party packages.
- The final rendered output for a resolved value uses its `str()` form; a missing
  value (and `None`) still renders as the empty string, as it does today (unless a
  `default` filter supplies a replacement).
