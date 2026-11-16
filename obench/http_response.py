"""Validate upstream response metadata before downstream serialization.

The stdlib HTTP parser preserves obsolete folds and some control characters.
RFC 9112 section 5.2 permits proxies to reject folded upstream fields with 502;
RFC 9110 section 5.5 requires rejecting or replacing unsafe field controls.
We reject rather than reinterpret malformed input or ambiguous framing.
"""

import http.client
import re

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
}
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)
_DECIMAL = re.compile(r"[0-9]+", re.ASCII)


def _validate_text(value: str) -> None:
    if any((ord(char) < 32 and char != "\t") or ord(char) == 127
           or ord(char) > 255 for char in value):
        raise ValueError("invalid upstream response control character")


def forward_response_headers(response: http.client.HTTPResponse) -> list[tuple[str, str]]:
    """Return validated end-to-end fields; preserve order/nonframing duplicates.

    Call before send_response or body reads. Only the transfer coding actually
    decoded by http.client is accepted. Identical Content-Length fields are
    reduced to one; conflicting, invalid or TE+CL framing is rejected.
    This validates parsed metadata, not every possible HTTP wire grammar.
    """
    _validate_text(response.reason)
    if response.headers.defects:
        raise ValueError("malformed upstream response headers")
    headers = response.getheaders()
    lengths = []
    transfers = []
    connection_tokens = set()
    for name, value in headers:
        if not _TOKEN.fullmatch(name):
            raise ValueError("invalid upstream response header name")
        _validate_text(value)
        low = name.lower()
        if low == "content-length":
            if not _DECIMAL.fullmatch(value.strip()):
                raise ValueError("invalid upstream Content-Length")
            lengths.append(int(value))
        elif low == "transfer-encoding":
            transfers.append(value)
        elif low == "connection":
            connection_tokens.update(token.strip().lower() for token in value.split(","))
    if lengths and (transfers or len(set(lengths)) != 1):
        raise ValueError("ambiguous upstream response framing")
    if transfers and (len(transfers) != 1 or transfers[0].lower() != "chunked"
                      or not response.chunked):
        raise ValueError("unsupported upstream transfer coding")
    blocked = HOP_BY_HOP | connection_tokens
    result = []
    length_sent = False
    for name, value in headers:
        low = name.lower()
        if low in blocked:
            continue
        if low == "content-length":
            if length_sent:
                continue
            length_sent = True
        result.append((name, value))
    return result
