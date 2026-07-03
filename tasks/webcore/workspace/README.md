# webcore

`webcore` is a small, in-process web framework. There is no network layer: a
`TestClient` drives an `App` entirely in memory, which makes it easy to build
and exercise request handling without a server.

## Module layout

```
webcore/
  converters.py       path-parameter handling (string segments today)
  routing.py          pattern parsing, Route, Router
  request.py          the Request value object (method, path, query, headers,
                      cookies, form/JSON body, Accept negotiation)
  response.py         Response, the case-insensitive Headers map,
                      text/html/json/redirect/no_content helpers, set_cookie
  middleware.py       the onion-model middleware chain (build_chain)
  app.py              App: routing + middleware + url_for
  testclient.py       TestClient: an in-process request driver
  status.py           HTTP status codes, reason phrases, classification helpers
  datastructures.py   MultiDict, ImmutableMultiDict, CaseInsensitiveDict
  exceptions.py       HTTPException hierarchy + abort()
  urls.py             URL encode/decode/join helpers
```

The core above is the part you edit for the routing/dispatch work. The framework
also ships a wider set of standalone, optional modules (import them directly,
e.g. `from webcore.sessions import SessionMiddleware`); none are wired into
dispatch automatically, and you should not need to touch them for this task:

```
webcore/
  signing.py            HMAC signing + timed tokens (Signer, TimedSigner)
  datetimeutil.py       HTTP-date formatting/parsing (http_date, parse_date_any)
  sessions.py           signed-cookie sessions (Session, SessionMiddleware)
  cookies.py            Cookie / CookieJar, parse_cookie / dump_cookie
  forms.py              urlencoded + multipart body parsing (FormData, FileStorage)
  negotiation.py        Accept content negotiation (Accept, best_media_type)
  jsonutil.py           JSON encoder for framework types (WebcoreJSONEncoder)
  templating.py         a tiny {{var}}/{%if%}/{%for%} renderer (Environment)
  renderers.py          pluggable, negotiated response renderers
  staticfiles.py        serve files from a directory (StaticFiles)
  ranges.py             HTTP Range / 206 Partial Content handling
  etag.py               ETags + conditional 304 (ConditionalMiddleware)
  caching.py            in-memory response cache + Cache-Control builder
  compression.py        gzip response middleware (GzipMiddleware)
  security.py           security-header middleware (SecurityHeaders, CSP)
  cors.py               CORS middleware (CORS)
  csrf.py               CSRF protection middleware (CSRFProtect)
  ratelimit.py          token-bucket rate limiting (RateLimitMiddleware)
  method_override.py    tunnel PUT/PATCH/DELETE through POST (MethodOverride)
  logging_middleware.py access logging / timing / request-id middleware
  debug.py              traceback capture + developer error page
  errorpages.py         negotiated error rendering (ErrorRegistry)
  blueprints.py         deferred route groups (Blueprint)
  views.py              class-based views (View, MethodView, TemplateView)
  events.py             synchronous signal/hook system (Signal, HookRegistry)
  config.py             layered application config (Config)
  validators.py         declarative input validation (Schema, Field)
  pagination.py         list-endpoint pagination helpers (Page, paginate)
  openapi.py            describe routes as an OpenAPI-ish document
  wsgi.py               WSGI adapter around App.dispatch (WSGIAdapter)
  testutil.py           extra TestClient assertion helpers (assert_that)
```

## Entry points

* **`App`** — create one, register routes with the `@app.route(path, methods=...)`
  decorator (or `app.add_route(name, path, handler, methods)`), add middleware
  with `app.use(mw)`, and turn a request into a response with `app.dispatch()`.
* **`TestClient`** — `TestClient(app)` exposes `.get/.post/.put/.patch/.delete/`
  `.head/.options` and a general `.request(method, path, follow_redirects=...)`,
  each returning a `Response`.
* **`url_for(name, **params)`** — `App.url_for` builds the URL string for a named
  route.

## Quick example

```python
from webcore import App, TestClient, text

app = App()

@app.route("/greet/{name}")
def greet(request, name):
    return text("hello " + name)

client = TestClient(app)
print(client.get("/greet/sam").text)   # -> "hello sam"
```

A handler takes the `request` plus one keyword argument per path parameter and
returns either a `Response` or a value the app coerces into one (a `str` becomes
`text/plain`, a `dict`/`list` becomes JSON).
