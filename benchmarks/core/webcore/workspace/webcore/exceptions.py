"""webcore.exceptions -- the HTTP exception hierarchy and ``abort``.

An :class:`HTTPException` carries a status code, an optional message, and
optional extra headers, and knows how to turn itself into a
:class:`~webcore.response.Response` via :meth:`HTTPException.to_response`. This
gives handlers a clean way to bail out::

    from webcore.exceptions import abort

    @app.route("/users/{id:int}")
    def get_user(request, id):
        user = lookup(id)
        if user is None:
            abort(404)
        return json_response(user)

Concrete subclasses (:class:`NotFound`, :class:`MethodNotAllowed`, ...) preset
the status code and default message; :func:`abort` is a convenience that raises
the right subclass for a code.
"""

from . import status as _status


class HTTPException(Exception):
    """Base class for all HTTP error signals.

    Parameters
    ----------
    status_code : int
        The HTTP status to report.
    description : str, optional
        Human-readable message; defaults to the status reason phrase.
    headers : mapping, optional
        Extra headers to attach to the generated response (e.g. ``Allow``).
    """

    status_code = 500

    def __init__(self, status_code=None, description=None, headers=None):
        if status_code is not None:
            self.status_code = status_code
        self.description = (
            description
            if description is not None
            else _status.reason_phrase(self.status_code)
        )
        self.headers = dict(headers) if headers else {}
        super().__init__("{} {}".format(self.status_code, self.description))

    def to_response(self):
        """Render this exception as a plain-text :class:`Response`."""
        # Imported lazily to avoid a circular import at module load.
        from .response import Response

        resp = Response(self.status_code, None, self.description)
        for key, value in self.headers.items():
            resp.headers[key] = value
        resp.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        return resp

    def __repr__(self):
        return "<{} {} {!r}>".format(
            type(self).__name__, self.status_code, self.description
        )


class BadRequest(HTTPException):
    status_code = 400


class Unauthorized(HTTPException):
    status_code = 401


class Forbidden(HTTPException):
    status_code = 403


class NotFound(HTTPException):
    status_code = 404


class MethodNotAllowed(HTTPException):
    """405 with the allowed methods recorded for an ``Allow`` header."""

    status_code = 405

    def __init__(self, allowed_methods=None, description=None):
        headers = {}
        if allowed_methods:
            headers["Allow"] = ", ".join(sorted(allowed_methods))
        super().__init__(self.status_code, description, headers)
        self.allowed_methods = sorted(allowed_methods or [])


class NotAcceptable(HTTPException):
    status_code = 406


class Conflict(HTTPException):
    status_code = 409


class Gone(HTTPException):
    status_code = 410


class UnsupportedMediaType(HTTPException):
    status_code = 415


class UnprocessableEntity(HTTPException):
    status_code = 422


class TooManyRequests(HTTPException):
    status_code = 429


class InternalServerError(HTTPException):
    status_code = 500


class NotImplementedError_(HTTPException):
    status_code = 501


# Map status codes to their dedicated exception classes for abort().
_BY_CODE = {
    cls.status_code: cls
    for cls in (
        BadRequest,
        Unauthorized,
        Forbidden,
        NotFound,
        MethodNotAllowed,
        NotAcceptable,
        Conflict,
        Gone,
        UnsupportedMediaType,
        UnprocessableEntity,
        TooManyRequests,
        InternalServerError,
        NotImplementedError_,
    )
}


def abort(status_code, description=None, headers=None):
    """Raise the :class:`HTTPException` subclass for ``status_code``.

    Falls back to a generic :class:`HTTPException` for codes without a dedicated
    subclass.
    """
    cls = _BY_CODE.get(status_code)
    if cls is MethodNotAllowed:
        raise MethodNotAllowed(description=description)
    if cls is not None:
        raise cls(status_code, description, headers)
    raise HTTPException(status_code, description, headers)
