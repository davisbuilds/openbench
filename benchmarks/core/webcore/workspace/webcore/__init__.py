"""webcore -- a small, in-process web framework.

webcore is a self-contained routing/middleware/response toolkit with no network
layer: a :class:`~webcore.testclient.TestClient` drives an
:class:`~webcore.app.App` entirely in memory. The public surface is re-exported
here for convenience::

    from webcore import App, TestClient, Response, text, json_response

Modules
-------
converters  path-parameter handling (string segments)
routing     pattern parsing, Route, Router
request     the Request value object
response    Response, Headers, and text/json/redirect helpers
middleware  the onion-model middleware chain
app         App: routing + middleware + url_for
testclient  TestClient: an in-process request driver
"""

from .app import App
from .request import Request
from .response import (
    Response,
    Headers,
    text,
    html,
    json_response,
    no_content,
    redirect,
)
from .routing import Router, Route, RouteError
from .middleware import build_chain, Middleware
from .testclient import TestClient
from .converters import Converter, StringConverter, get_converter
from .datastructures import MultiDict, ImmutableMultiDict, CaseInsensitiveDict
from .exceptions import (
    HTTPException,
    BadRequest,
    Unauthorized,
    Forbidden,
    NotFound,
    MethodNotAllowed,
    Conflict,
    Gone,
    UnprocessableEntity,
    abort,
)
from . import status, urls

__all__ = [
    "App",
    "Request",
    "Response",
    "Headers",
    "text",
    "html",
    "json_response",
    "no_content",
    "redirect",
    "Router",
    "Route",
    "RouteError",
    "build_chain",
    "Middleware",
    "TestClient",
    "Converter",
    "StringConverter",
    "get_converter",
    "MultiDict",
    "ImmutableMultiDict",
    "CaseInsensitiveDict",
    "HTTPException",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "MethodNotAllowed",
    "Conflict",
    "Gone",
    "UnprocessableEntity",
    "abort",
    "status",
    "urls",
]

__version__ = "0.9.0"
