"""webcore.forms -- request-body form parsing.

Handlers usually want form fields as a tidy mapping, not a raw body string. This
module parses the two content types browsers actually submit:

*   ``application/x-www-form-urlencoded`` -- the default ``<form>`` encoding, a
    query-string in the body.
*   ``multipart/form-data`` -- the file-upload encoding, a MIME-ish body split by
    a ``boundary`` into parts, each with its own headers.

Both parse into a :class:`FormData` (a multi-valued mapping of text fields) plus,
for multipart, a matching set of :class:`FileStorage` objects. Everything works
on ``str``/``bytes`` in memory -- there is no streaming layer -- which is exactly
right for an in-process framework driven by :class:`~webcore.testclient.TestClient`.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import parse_qsl

from .datastructures import MultiDict

__all__ = [
    "FormData",
    "FileStorage",
    "parse_urlencoded",
    "parse_multipart",
    "parse_form",
    "parse_options_header",
]


def parse_options_header(value: str) -> Tuple[str, Dict[str, str]]:
    """Split a header like ``multipart/form-data; boundary=xyz`` into its parts.

    Returns ``(main_value, params)`` where ``params`` maps each ``key=value``
    option (unquoted) with lower-cased keys. Empty input yields ``("", {})``.
    """
    if not value:
        return "", {}
    parts = value.split(";")
    main = parts[0].strip()
    params: Dict[str, str] = {}
    for part in parts[1:]:
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, raw = part.partition("=")
        params[key.strip().lower()] = raw.strip().strip('"')
    return main, params


class FileStorage:
    """One uploaded file from a ``multipart/form-data`` body.

    Parameters mirror the part's ``Content-Disposition``/``Content-Type``: the
    form field :attr:`name`, the client's :attr:`filename`, the declared
    :attr:`content_type`, and the raw :attr:`data` bytes.
    """

    def __init__(self, name: str, filename: str, content_type: str,
                 data: bytes) -> None:
        self.name = name
        self.filename = filename
        self.content_type = content_type or "application/octet-stream"
        self.data = data

    @property
    def size(self) -> int:
        """The uploaded payload's length in bytes."""
        return len(self.data)

    def read(self) -> bytes:
        """Return the raw bytes (the whole file is already in memory)."""
        return self.data

    def text(self, encoding: str = "utf-8") -> str:
        """Decode the payload as text."""
        return self.data.decode(encoding)

    def save(self, path: str) -> None:
        """Write the payload to ``path`` on disk."""
        with open(path, "wb") as fh:
            fh.write(self.data)

    def __repr__(self) -> str:
        return "<FileStorage {!r} ({} bytes)>".format(self.filename, self.size)


class FormData(MultiDict):
    """A :class:`~webcore.datastructures.MultiDict` of submitted text fields.

    Uploaded files, when present, live in the companion :attr:`files` mapping so
    text access (``form["email"]``) stays clean and typed as ``str``.
    """

    def __init__(self, fields: Optional[Iterable] = None,
                 files: Optional[Dict[str, FileStorage]] = None) -> None:
        super().__init__(fields or [])
        self.files: MultiDict = MultiDict()
        if files:
            for name, storage in (files.items() if hasattr(files, "items") else files):
                self.files.add(name, storage)

    def get_file(self, name: str) -> Optional[FileStorage]:
        """Return the first uploaded file for ``name`` (or ``None``)."""
        return self.files.get(name)

    def get_files(self, name: str) -> List[FileStorage]:
        """Return every uploaded file submitted under ``name``."""
        return self.files.getlist(name)

    def __repr__(self) -> str:
        return "FormData(fields={!r}, files={!r})".format(
            self.items(multi=True), self.files.keys()
        )


def _as_text(body: Union[str, bytes], encoding: str = "utf-8") -> str:
    if isinstance(body, bytes):
        return body.decode(encoding)
    return body or ""


def _as_bytes(body: Union[str, bytes], encoding: str = "utf-8") -> bytes:
    if isinstance(body, str):
        return body.encode(encoding)
    return body or b""


def parse_urlencoded(body: Union[str, bytes], encoding: str = "utf-8") -> FormData:
    """Parse an ``application/x-www-form-urlencoded`` body into :class:`FormData`.

    Blank values are kept (``?flag`` -> ``flag=""``) and repeated keys preserve
    all their values, matching how :class:`webcore.request.Request` treats query
    strings.
    """
    text = _as_text(body, encoding)
    pairs = parse_qsl(text, keep_blank_values=True)
    return FormData(pairs)


def _split_multipart(body: bytes, boundary: bytes) -> List[bytes]:
    """Split a multipart body on its boundary delimiter, dropping the epilogue."""
    delimiter = b"--" + boundary
    segments = body.split(delimiter)
    parts = []
    for segment in segments:
        # The preamble (before the first boundary) and the terminator ("--")
        # produce empty or "--"-prefixed segments we skip.
        if segment in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if segment.startswith(b"--"):
            continue
        parts.append(segment.strip(b"\r\n"))
    return parts


def _parse_part_headers(raw_headers: bytes) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for line in raw_headers.split(b"\r\n"):
        if not line or b":" not in line:
            continue
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def parse_multipart(body: Union[str, bytes], boundary: str,
                    encoding: str = "utf-8") -> FormData:
    """Parse a ``multipart/form-data`` body given its ``boundary``.

    Each part's ``Content-Disposition`` supplies the field ``name`` and, for
    files, the ``filename``; a part with a filename becomes a
    :class:`FileStorage` in :attr:`FormData.files`, otherwise its decoded text
    becomes a normal field.
    """
    raw = _as_bytes(body, encoding)
    boundary_bytes = boundary.encode("latin-1")
    form = FormData()
    for part in _split_multipart(raw, boundary_bytes):
        head, _, payload = part.partition(b"\r\n\r\n")
        headers = _parse_part_headers(head)
        disposition = headers.get("content-disposition", "")
        _main, params = parse_options_header(disposition)
        field_name = params.get("name")
        if field_name is None:
            continue
        filename = params.get("filename")
        if filename is not None:
            storage = FileStorage(
                field_name,
                filename,
                headers.get("content-type", ""),
                payload,
            )
            form.files.add(field_name, storage)
        else:
            form.add(field_name, payload.decode(encoding))
    return form


def parse_form(body: Union[str, bytes], content_type: str,
               encoding: str = "utf-8") -> FormData:
    """Dispatch to the right parser based on ``content_type``.

    An unrecognised content type yields an empty :class:`FormData` rather than
    raising, so a handler can call this unconditionally.
    """
    mimetype, params = parse_options_header(content_type)
    if mimetype == "application/x-www-form-urlencoded":
        return parse_urlencoded(body, encoding)
    if mimetype == "multipart/form-data":
        boundary = params.get("boundary")
        if not boundary:
            return FormData()
        return parse_multipart(body, boundary, encoding)
    return FormData()
