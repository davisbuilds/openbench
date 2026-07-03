# Add typed routing, mounting, and content-negotiation to webcore

`webcore` is our small in-process web framework (see `webcore/README.md` for the
module layout and entry points). It already handles the basics and we want to
grow its routing layer into something production-shaped. This is a single,
connected piece of work: converters, routing precedence, sub-application
mounting, and HTTP method handling all interact, so please implement the whole
thing coherently rather than in isolated patches.

## What already works (do not break it)

The following behaviour exists today and must keep working exactly as it does
now:

* **Static routing** — `@app.route("/items")` matches `GET /items`.
* **A single string path parameter** — `@app.route("/greet/{name}")` captures one
  non-empty segment (no `/`) and passes it to the handler as `name="..."`.
  A handler is called as `handler(request, **path_params)`.
* **GET/POST dispatch** — a route declares `methods=[...]` (default `["GET"]`);
  the matching route for the request's method is invoked.
* **Responses** — `text(body, status=200, headers=None)` returns a
  `text/plain; charset=utf-8` response; `json_response(obj)` returns
  `application/json`. A handler may also return a bare `str` (becomes text) or a
  `dict`/`list` (becomes JSON). `Response` exposes `.status`, `.headers` (a
  case-insensitive map), `.text`, and `.json()`.
* **Request query parsing** — `Request(method, path)` splits any `?query` off the
  path, exposing `.path`, `.query_string`, and `.query` (a dict, last value wins,
  percent- and `+`-decoded).
* **The middleware onion (single app)** — `app.use(mw)` registers a middleware
  `mw(request, next) -> response`. Middlewares run outermost-first on the way in
  and unwind on the way out; the first registered is the outermost. A middleware
  that returns a response without calling `next` short-circuits the inner layers
  and the handler.
* **404** — a request whose path matches no route returns status 404.
* **TestClient** — `TestClient(app)` with `.get/.post/...` and
  `.request(method, path, follow_redirects=False)`.

Keep all of that intact. The work below extends the routing and dispatch layers
around it.

## What to build

### 1. Typed path converters

Support typed placeholders in route patterns. A placeholder always occupies a
whole path segment and is written `{name}` or `{name:converter}`:

* `{name}` — the existing behaviour: exactly one non-empty segment, no `/`,
  delivered as a `str`. This is the `str` converter and stays the default.
* `{id:int}` — one or more ASCII digits (`[0-9]+`) **only**. The handler receives
  a Python `int`.
* `{s:slug}` — matches `[a-z0-9]+(?:-[a-z0-9]+)*` (lowercase alphanumerics joined
  by single hyphens). Delivered as a `str`.
* `{rest:path}` — one or more segments **including `/`**. Delivered as a `str`
  that may contain `/`. Because it swallows slashes it is only legal as the
  **last** segment of a pattern; a `path` converter anywhere else is an error.

### 2. Converter fall-through

Converter matching is part of route matching. If a segment fails its converter
(for example `abc` against `{id:int}`), that route simply does **not** match, and
matching continues to the remaining routes. If no route matches, the result is a
404 — a non-matching converter must never raise or produce a 500.

### 3. Route precedence

When more than one route could match a path, resolve the ambiguity by
specificity, not by luck of registration order:

* At any given position, a **static** segment beats a **dynamic** one.
* More-static routes are tried before more-dynamic ones.
* A `{...:path}` route has the **lowest** precedence of all.
* A genuine tie (same shape) is broken by registration order (earlier wins).

So with `/users/{id:int}` and `/users/new` both registered, `GET /users/new`
hits the static route and `GET /users/7` hits the dynamic one — regardless of
which was registered first.

### 4. Handler parameter types

The value a handler receives matches the converter: `int` parameters arrive as
`int`, `str` and `slug` parameters as `str`, and a `path` parameter as a `str`
that may contain `/`.

### 5. Sub-application mounting

Add `app.mount(prefix, subapp)`. The `prefix` is itself a route pattern and may
contain parameters, e.g. `app.mount("/api/{ver}", subapp)`. A request whose path
starts with the prefix is routed into the sub-application against the remainder
of the path. The handler receives **both** the prefix parameters and the
sub-route parameters, merged into one set of keyword arguments. For example, with
`sub` holding `@sub.route("/users/{id:int}")` and `app.mount("/api/{ver}", sub)`,
`GET /api/v2/users/9` invokes the handler with `ver="v2"` and `id=9`. Mounts may
nest.

### 6. Middleware ordering across a mount

Middleware follows the same onion model through a mount. On the way in, the
parent app's middlewares run first, then the mounted sub-app's middlewares, then
the handler; on the way out it unwinds child-then-parent. A middleware that
returns a response without calling `next` still short-circuits everything inside
it — including a mounted sub-app's middlewares and handler.

### 7. Trailing-slash redirect

A route has one canonical form: whatever it was registered as. A request for the
other trailing-slash form redirects to the canonical one with a **308** and a
`Location` header pointing at the canonical path. Any query string is preserved
on the redirect target.

* Registered `/items`, request `GET /items/` → 308, `Location: /items`.
* Registered `/items`, request `GET /items/?a=1&b=2` → 308,
  `Location: /items?a=1&b=2`.
* Registered `/dir/`, request `GET /dir` → 308, `Location: /dir/`.

### 8. Method mismatch → 405

If a path matches a route but none of the routes on that path allow the
request's method, return **405** (not 404) with an `Allow` header whose value is
the allowed methods for that path, **sorted and comma-separated** (e.g.
`DELETE, OPTIONS, POST`). The allowed set includes the auto methods from clause
10.

### 9. `url_for(name, **params)`

Reverse-generate a URL for a named route:

* Fill the route's parameters from `params`, formatting each through its
  converter (an `int` renders as its decimal string; a `slug`/`str` is validated
  against its pattern).
* Include the mount prefix, filled with its own parameters, when the named route
  lives inside a mounted sub-app.
* Any keyword arguments left over after the path is filled become a query string:
  URL-encoded, **sorted by key**, and appended after `?`.
* A missing or invalid parameter raises a clear error (`KeyError` or
  `ValueError`).

For example, `url_for("user", id=7, q="hello world", page=2)` for
`/users/{id:int}` named `user` yields `/users/7?page=2&q=hello+world`.

### 10. Automatic HEAD and OPTIONS

* A route registered for `GET` also answers `HEAD`: same status and headers as
  the `GET` response, but an **empty body**.
* `OPTIONS` on a path that matches at least one route returns **204** with an
  `Allow` header (sorted, comma-separated) listing the methods that path
  supports — including the auto `HEAD` (when `GET` is present) and `OPTIONS`
  itself.

Implement all ten together so the pieces compose: a mounted, parametric route
with a typed parameter should still honour precedence, method handling, and
`url_for`.
