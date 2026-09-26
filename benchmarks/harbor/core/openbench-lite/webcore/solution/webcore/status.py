"""webcore.status -- HTTP status codes and their reason phrases.

A single source of truth for the numeric codes webcore knows about, their
canonical reason phrases, and a handful of classification helpers
(:func:`is_success`, :func:`is_redirect`, ...). :class:`~webcore.response.Response`
uses :func:`reason_phrase` to fill in a status line's text.

The table is intentionally broad -- more codes than webcore emits itself -- so
that application handlers returning, say, ``201`` or ``422`` still get a sensible
reason phrase.
"""


#: Full status-code -> reason-phrase table.
PHRASES = {
    # 1xx informational
    100: "Continue",
    101: "Switching Protocols",
    102: "Processing",
    103: "Early Hints",
    # 2xx success
    200: "OK",
    201: "Created",
    202: "Accepted",
    203: "Non-Authoritative Information",
    204: "No Content",
    205: "Reset Content",
    206: "Partial Content",
    207: "Multi-Status",
    208: "Already Reported",
    226: "IM Used",
    # 3xx redirection
    300: "Multiple Choices",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    305: "Use Proxy",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    # 4xx client error
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Payload Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    417: "Expectation Failed",
    418: "I'm a Teapot",
    421: "Misdirected Request",
    422: "Unprocessable Entity",
    423: "Locked",
    424: "Failed Dependency",
    425: "Too Early",
    426: "Upgrade Required",
    428: "Precondition Required",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    451: "Unavailable For Legal Reasons",
    # 5xx server error
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
    507: "Insufficient Storage",
    508: "Loop Detected",
    511: "Network Authentication Required",
}


def reason_phrase(code):
    """Return the reason phrase for ``code`` (empty string if unknown)."""
    return PHRASES.get(int(code), "")


def is_informational(code):
    """True for a 1xx status code."""
    return 100 <= int(code) < 200


def is_success(code):
    """True for a 2xx status code."""
    return 200 <= int(code) < 300


def is_redirect(code):
    """True for a 3xx status code."""
    return 300 <= int(code) < 400


def is_client_error(code):
    """True for a 4xx status code."""
    return 400 <= int(code) < 500


def is_server_error(code):
    """True for a 5xx status code."""
    return 500 <= int(code) < 600


def is_error(code):
    """True for any 4xx or 5xx status code."""
    return is_client_error(code) or is_server_error(code)


def has_body(code):
    """False for status codes that must not carry a response body.

    ``204 No Content``, ``304 Not Modified``, and every 1xx code are body-less.
    """
    code = int(code)
    return not (code == 204 or code == 304 or is_informational(code))
