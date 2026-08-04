"""webcore.signing -- HMAC message signing and timed tokens.

This module gives the rest of webcore a small, dependency-free toolkit for
authenticating opaque strings: cookie session payloads
(:mod:`webcore.sessions`), password-reset style links, CSRF tokens, and any
other value the framework hands to a client and later trusts on the way back
in.

The design borrows the *ideas* every framework converges on -- a keyed hash, a
constant-time comparison, and URL-safe base64 -- but the wire format here is
webcore's own::

    payload "." signature                       (Signer)
    payload "." timestamp "." signature         (TimedSigner)

where ``payload`` and ``timestamp`` are base64url without padding and
``signature`` is the base64url HMAC over ``payload`` (or ``payload.timestamp``).
Nothing here encrypts: a signed value is *readable* by the client, only not
*forgeable* without the key.

Example
-------
::

    signer = Signer("secret-key")
    token = signer.sign("user:42")          # -> "dXNlcjo0Mg.<sig>"
    signer.unsign(token)                     # -> "user:42"

    timed = TimedSigner("secret-key")
    tok = timed.sign("reset:42")
    timed.unsign(tok, max_age=3600)          # raises SignatureExpired after 1h
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Callable, Iterable, Optional, Union

__all__ = [
    "BadSignature",
    "SignatureExpired",
    "b64encode",
    "b64decode",
    "constant_time_compare",
    "derive_key",
    "Signer",
    "TimedSigner",
]


#: Default separator between a payload and its signature.
DEFAULT_SEP = "."
#: Default digest used for every HMAC in this module.
DEFAULT_DIGEST = "sha256"


class BadSignature(Exception):
    """Raised when a signature does not match the expected value.

    The offending ``payload`` (the part that *was* recovered, if any) is kept on
    the exception so a caller can log it without re-parsing the token.
    """

    def __init__(self, message: str, payload: Optional[str] = None) -> None:
        super().__init__(message)
        self.payload = payload


class SignatureExpired(BadSignature):
    """A :class:`BadSignature` specialisation for tokens past their ``max_age``.

    :attr:`age` is the token's age in whole seconds at the moment verification
    failed, which callers commonly surface in a "link expired" message.
    """

    def __init__(self, message: str, payload: Optional[str] = None,
                 age: Optional[int] = None) -> None:
        super().__init__(message, payload)
        self.age = age


def b64encode(raw: bytes) -> str:
    """URL-safe base64 with the ``=`` padding stripped.

    Padding is deterministic given the input length, so dropping it keeps tokens
    short without losing information; :func:`b64decode` restores it.
    """
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    return encoded.decode("ascii")


def b64decode(value: str) -> bytes:
    """Reverse :func:`b64encode`, restoring the stripped ``=`` padding."""
    if isinstance(value, bytes):
        value = value.decode("ascii")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise BadSignature("invalid base64 payload") from exc


def constant_time_compare(left: Union[str, bytes], right: Union[str, bytes]) -> bool:
    """Compare two values without leaking their relationship through timing.

    A thin, well-named wrapper over :func:`hmac.compare_digest` that also accepts
    ``str`` operands (encoded as UTF-8) so call sites do not have to remember to
    encode first.
    """
    if isinstance(left, str):
        left = left.encode("utf-8")
    if isinstance(right, str):
        right = right.encode("utf-8")
    return hmac.compare_digest(left, right)


def derive_key(secret: Union[str, bytes], salt: Union[str, bytes] = b"") -> bytes:
    """Mix a ``secret`` and a ``salt`` into one 32-byte key.

    Different framework subsystems (sessions, CSRF, remember-me cookies) should
    not share a raw key even when the user configures a single application
    secret; deriving a namespaced key per use is the conventional defence. The
    derivation is a single keyed hash -- enough to separate namespaces, not a
    password KDF.
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return hmac.new(secret, b"webcore-signer" + salt, hashlib.sha256).digest()


class Signer:
    """Sign and verify strings with a keyed HMAC.

    Parameters
    ----------
    secret:
        The application secret. Combined with ``salt`` through
        :func:`derive_key`, so two signers with the same secret but different
        salts produce mutually-unverifiable tokens.
    salt:
        A namespace for this signer (e.g. ``"session"`` or ``"csrf"``).
    sep:
        The character between payload and signature. Must not appear in the
        base64url alphabet; the default ``"."`` satisfies that.
    digest:
        Name of a :mod:`hashlib` algorithm for the HMAC (default ``"sha256"``).
    """

    def __init__(self, secret: Union[str, bytes], salt: Union[str, bytes] = "signer",
                 sep: str = DEFAULT_SEP, digest: str = DEFAULT_DIGEST) -> None:
        if sep in _BASE64_ALPHABET:
            raise ValueError("separator {!r} clashes with the base64url alphabet".format(sep))
        self.key = derive_key(secret, salt)
        self.sep = sep
        self.digest = digest

    # -- primitives ------------------------------------------------------

    def signature(self, value: Union[str, bytes]) -> str:
        """Return the base64url HMAC of ``value`` (without the payload)."""
        if isinstance(value, str):
            value = value.encode("utf-8")
        algo = getattr(hashlib, self.digest)
        mac = hmac.new(self.key, value, algo).digest()
        return b64encode(mac)

    def verify_signature(self, value: Union[str, bytes], signature: str) -> bool:
        """True if ``signature`` is the correct HMAC for ``value``."""
        return constant_time_compare(signature, self.signature(value))

    # -- string API ------------------------------------------------------

    def sign(self, value: str) -> str:
        """Return ``value`` base64url-encoded and appended with its signature."""
        payload = b64encode(value.encode("utf-8"))
        return payload + self.sep + self.signature(payload)

    def unsign(self, signed: str) -> str:
        """Verify ``signed`` and return the original string.

        Raises :class:`BadSignature` when the token is malformed or the signature
        does not check out.
        """
        if self.sep not in signed:
            raise BadSignature("no separator {!r} in token".format(self.sep))
        payload, _, signature = signed.rpartition(self.sep)
        if not self.verify_signature(payload, signature):
            raise BadSignature("signature mismatch", payload=payload)
        return b64decode(payload).decode("utf-8")

    def validate(self, signed: str) -> bool:
        """Return whether ``signed`` verifies, swallowing :class:`BadSignature`."""
        try:
            self.unsign(signed)
        except BadSignature:
            return False
        return True

    def __repr__(self) -> str:
        return "<Signer digest={!r}>".format(self.digest)


class TimedSigner(Signer):
    """A :class:`Signer` that also embeds and checks a timestamp.

    The signed form is ``payload.timestamp.signature`` where the HMAC covers
    ``payload.timestamp`` together, so neither the value nor its age can be
    tampered with independently.
    """

    def __init__(self, secret: Union[str, bytes], salt: Union[str, bytes] = "timed-signer",
                 sep: str = DEFAULT_SEP, digest: str = DEFAULT_DIGEST,
                 time_func: Callable[[], float] = time.time) -> None:
        super().__init__(secret, salt, sep, digest)
        self._time = time_func

    def _encode_timestamp(self, when: float) -> str:
        return b64encode(str(int(when)).encode("ascii"))

    def _decode_timestamp(self, encoded: str) -> int:
        return int(b64decode(encoded).decode("ascii"))

    def sign(self, value: str) -> str:
        payload = b64encode(value.encode("utf-8"))
        timestamp = self._encode_timestamp(self._time())
        body = payload + self.sep + timestamp
        return body + self.sep + self.signature(body)

    def unsign(self, signed: str, max_age: Optional[float] = None,
               return_timestamp: bool = False):
        """Verify ``signed`` and return the original value.

        Parameters
        ----------
        max_age:
            If given, a token older than this many seconds raises
            :class:`SignatureExpired`.
        return_timestamp:
            When true, return ``(value, issued_at_unixseconds)`` instead of just
            the value.
        """
        try:
            body, _, signature = signed.rpartition(self.sep)
            payload, _, timestamp = body.rpartition(self.sep)
        except ValueError as exc:  # pragma: no cover - rpartition never raises
            raise BadSignature("malformed timed token") from exc
        if not payload or not timestamp:
            raise BadSignature("malformed timed token")
        if not self.verify_signature(body, signature):
            raise BadSignature("signature mismatch", payload=payload)
        issued = self._decode_timestamp(timestamp)
        if max_age is not None:
            age = int(self._time()) - issued
            if age > max_age:
                raise SignatureExpired(
                    "token is {}s old (max {}s)".format(age, int(max_age)),
                    payload=payload,
                    age=age,
                )
            if age < 0:
                raise SignatureExpired("token timestamp is in the future", payload=payload, age=age)
        value = b64decode(payload).decode("utf-8")
        if return_timestamp:
            return value, issued
        return value


def sign_all(signer: Signer, values: Iterable[str]) -> list:
    """Sign every value in ``values`` with ``signer`` (a small batch helper)."""
    return [signer.sign(v) for v in values]


#: The base64url alphabet, used to reject a clashing separator early.
_BASE64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
)
