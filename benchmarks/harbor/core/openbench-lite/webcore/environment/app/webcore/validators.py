"""webcore.validators -- declarative validation for form and JSON input.

A small, original validation layer: declare a :class:`Schema` mapping field
names to :class:`Field` objects, then :meth:`Schema.validate` a mapping (a parsed
form or JSON body) into cleaned values, collecting per-field errors instead of
raising on the first failure. Individual :class:`Validator` callables express the
rules (required, length, range, pattern, choice), and fields compose them with a
type coercion.

This is not a serialization framework -- it validates and coerces flat inputs,
which is what a request handler usually needs before doing work.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ValidationError",
    "Validator",
    "Required",
    "Length",
    "NumberRange",
    "Pattern",
    "OneOf",
    "Email",
    "Field",
    "Schema",
]


class ValidationError(Exception):
    """Raised by a validator when a value fails its rule.

    Carries a human-readable ``message``; :class:`Schema` catches these and maps
    them to the offending field name instead of propagating.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Validator:
    """Base class for a single validation rule.

    Subclasses implement ``__call__(value) -> None`` and raise
    :class:`ValidationError` on failure. Validators are stateless and reusable
    across fields.
    """

    def __call__(self, value: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class Required(Validator):
    """Reject ``None`` and empty strings/collections."""

    def __init__(self, message: str = "this field is required") -> None:
        self.message = message

    def __call__(self, value: Any) -> None:
        if value is None or (isinstance(value, (str, list, tuple, dict)) and len(value) == 0):
            raise ValidationError(self.message)


class Length(Validator):
    """Constrain the length of a string (or sized value)."""

    def __init__(self, min: int = 0, max: Optional[int] = None,
                 message: Optional[str] = None) -> None:
        self.min = min
        self.max = max
        self.message = message

    def __call__(self, value: Any) -> None:
        length = len(value)
        if length < self.min:
            raise ValidationError(
                self.message or "must be at least {} characters".format(self.min))
        if self.max is not None and length > self.max:
            raise ValidationError(
                self.message or "must be at most {} characters".format(self.max))


class NumberRange(Validator):
    """Constrain a numeric value between optional bounds (inclusive)."""

    def __init__(self, min: Optional[float] = None, max: Optional[float] = None,
                 message: Optional[str] = None) -> None:
        self.min = min
        self.max = max
        self.message = message

    def __call__(self, value: Any) -> None:
        if self.min is not None and value < self.min:
            raise ValidationError(self.message or "must be >= {}".format(self.min))
        if self.max is not None and value > self.max:
            raise ValidationError(self.message or "must be <= {}".format(self.max))


class Pattern(Validator):
    """Require a string to match a regular expression."""

    def __init__(self, pattern: str, message: str = "invalid format") -> None:
        self.regex = re.compile(pattern)
        self.message = message

    def __call__(self, value: Any) -> None:
        if self.regex.match(value) is None:
            raise ValidationError(self.message)


class OneOf(Validator):
    """Require the value to be one of an allowed set."""

    def __init__(self, choices: Sequence[Any], message: Optional[str] = None) -> None:
        self.choices = list(choices)
        self.message = message

    def __call__(self, value: Any) -> None:
        if value not in self.choices:
            raise ValidationError(
                self.message or "must be one of {}".format(self.choices))


class Email(Validator):
    """A pragmatic (not RFC-exhaustive) email-shape check."""

    _RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def __init__(self, message: str = "invalid email address") -> None:
        self.message = message

    def __call__(self, value: Any) -> None:
        if self._RE.match(value) is None:
            raise ValidationError(self.message)


class Field:
    """One schema field: a type coercion plus an ordered list of validators.

    Parameters
    ----------
    type:
        A callable coercing the raw value (``str``, ``int``, ``float``, ``bool``,
        or any one-arg callable). Coercion failure yields a field error.
    required:
        Shortcut that prepends a :class:`Required` validator.
    default:
        Value substituted when the field is absent (and not required).
    validators:
        Additional :class:`Validator` rules applied after coercion.
    """

    def __init__(self, type: Callable[[Any], Any] = str, required: bool = False,
                 default: Any = None, validators: Sequence[Validator] = ()) -> None:
        self.type = type
        self.required = required
        self.default = default
        self.validators: List[Validator] = []
        if required:
            self.validators.append(Required())
        self.validators.extend(validators)

    def process(self, present: bool, raw: Any) -> Tuple[Any, List[str]]:
        """Coerce and validate one raw value.

        Returns ``(cleaned_value, errors)``. An absent, non-required field yields
        its default with no errors; an absent required field yields one error.
        """
        errors: List[str] = []
        if not present:
            if self.required:
                return None, ["this field is required"]
            return self.default, errors
        try:
            value = self._coerce(raw)
        except (TypeError, ValueError):
            return None, ["invalid value"]
        for validator in self.validators:
            try:
                validator(value)
            except ValidationError as exc:
                errors.append(exc.message)
        return value, errors

    def _coerce(self, raw: Any) -> Any:
        if self.type is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return self.type(raw)


class Schema:
    """A named set of :class:`Field` definitions validated as a unit.

    :meth:`validate` returns ``(cleaned, errors)``: ``cleaned`` maps each field
    to its coerced value and ``errors`` maps only the failing fields to their
    message lists. ``errors`` is empty exactly when the input is valid.
    """

    def __init__(self, fields: Mapping[str, Field]) -> None:
        self.fields: Dict[str, Field] = dict(fields)

    def validate(self, data: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
        cleaned: Dict[str, Any] = {}
        errors: Dict[str, List[str]] = {}
        for name, field in self.fields.items():
            present = name in data
            raw = data.get(name)
            value, field_errors = field.process(present, raw)
            if field_errors:
                errors[name] = field_errors
            else:
                cleaned[name] = value
        return cleaned, errors

    def is_valid(self, data: Mapping[str, Any]) -> bool:
        """True if ``data`` passes every field's rules."""
        _cleaned, errors = self.validate(data)
        return not errors

    def field_names(self) -> List[str]:
        return list(self.fields.keys())

    def __repr__(self) -> str:
        return "<Schema fields={}>".format(list(self.fields))
