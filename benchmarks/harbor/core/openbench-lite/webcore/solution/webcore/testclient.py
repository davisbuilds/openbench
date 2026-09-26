"""webcore.testclient -- an in-process client for driving an :class:`App`.

No sockets are involved: :meth:`TestClient.request` builds a
:class:`~webcore.request.Request`, calls :meth:`App.dispatch`, and returns the
:class:`~webcore.response.Response`. The per-method shortcuts (:meth:`get`,
:meth:`post`, ...) forward to :meth:`request`.

``follow_redirects`` chases ``301/302/303/307/308`` responses. Method
preservation follows HTTP semantics: ``307`` and ``308`` keep the original
method (so a redirected ``POST`` stays a ``POST``), while ``301/302/303``
downgrade to ``GET``.
"""

from .request import Request


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_METHOD_PRESERVING = frozenset({307, 308})


class TestClient:
    """Drives an :class:`~webcore.app.App` in-process."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, headers=None, body=b"", follow_redirects=False):
        """Dispatch one request and return the :class:`Response`.

        With ``follow_redirects=True`` a redirect is chased (up to a small hop
        limit) to its final response.
        """
        response = self._dispatch(method, path, headers, body)
        if not follow_redirects:
            return response

        hops = 0
        while response.status in _REDIRECT_STATUSES and "Location" in response.headers:
            if hops >= 10:
                break
            hops += 1
            location = response.headers["Location"]
            next_method = method if response.status in _METHOD_PRESERVING else "GET"
            next_body = body if response.status in _METHOD_PRESERVING else b""
            response = self._dispatch(next_method, location, headers, next_body)
        return response

    def _dispatch(self, method, path, headers, body):
        return self.app.dispatch(Request(method, path, headers, body))

    # -- per-method shortcuts -------------------------------------------

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def head(self, path, **kwargs):
        return self.request("HEAD", path, **kwargs)

    def options(self, path, **kwargs):
        return self.request("OPTIONS", path, **kwargs)
