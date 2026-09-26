"""webcore.events -- a small synchronous signal/hook system.

Frameworks need lifecycle extension points -- "before request", "after
response", "on error" -- without hard-wiring callers to callees. This module
provides a minimal synchronous dispatcher: named :class:`Signal` objects that
handlers :meth:`connect` to and that callers :meth:`send`, plus a
:class:`HookRegistry` that groups the request-lifecycle signals an app would
expose.

It is intentionally synchronous and in-process: sending a signal calls every
connected receiver in registration order and collects their return values. No
threading, no weak references beyond an opt-in, no async.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["Signal", "HookRegistry", "before_request", "after_request"]

Receiver = Callable[..., Any]


class Signal:
    """A named event that receivers connect to and callers send.

    Receivers are plain callables invoked with whatever positional/keyword
    arguments :meth:`send` is given. :meth:`send` returns a list of
    ``(receiver, result)`` pairs in connection order.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._receivers: List[Receiver] = []

    def connect(self, receiver: Receiver) -> Receiver:
        """Register ``receiver``; returns it so it works as a decorator."""
        if receiver not in self._receivers:
            self._receivers.append(receiver)
        return receiver

    def disconnect(self, receiver: Receiver) -> bool:
        """Remove ``receiver``; return whether it was connected."""
        try:
            self._receivers.remove(receiver)
            return True
        except ValueError:
            return False

    def send(self, *args: Any, **kwargs: Any) -> List[Tuple[Receiver, Any]]:
        """Invoke every receiver in order, collecting ``(receiver, result)``."""
        results = []
        for receiver in list(self._receivers):
            results.append((receiver, receiver(*args, **kwargs)))
        return results

    def send_until(self, *args: Any, **kwargs: Any) -> Optional[Any]:
        """Invoke receivers until one returns a truthy value; return it (or None).

        Useful for "first handler that produces a response wins" semantics.
        """
        for receiver in list(self._receivers):
            result = receiver(*args, **kwargs)
            if result:
                return result
        return None

    @property
    def receivers(self) -> List[Receiver]:
        """A copy of the connected receivers."""
        return list(self._receivers)

    def has_receivers(self) -> bool:
        return bool(self._receivers)

    def __len__(self) -> int:
        return len(self._receivers)

    def __repr__(self) -> str:
        return "<Signal {!r} ({} receivers)>".format(self.name, len(self._receivers))


class HookRegistry:
    """A named collection of :class:`Signal` objects.

    Provides the lifecycle signals an application typically exposes
    (``before_request``, ``after_request``, ``on_error``, ``teardown``) and lets
    callers register more by name. Retrieving an unknown signal creates it
    lazily, so plugins can connect before the core defines it.
    """

    DEFAULT_SIGNALS = ("before_request", "after_request", "on_error", "teardown")

    def __init__(self) -> None:
        self._signals: Dict[str, Signal] = {}
        for name in self.DEFAULT_SIGNALS:
            self._signals[name] = Signal(name)

    def signal(self, name: str) -> Signal:
        """Return the named signal, creating it on first access."""
        sig = self._signals.get(name)
        if sig is None:
            sig = Signal(name)
            self._signals[name] = sig
        return sig

    def connect(self, name: str, receiver: Receiver) -> Receiver:
        """Connect ``receiver`` to the named signal."""
        return self.signal(name).connect(receiver)

    def on(self, name: str) -> Callable[[Receiver], Receiver]:
        """Decorator that connects the decorated function to ``name``."""
        def decorator(func: Receiver) -> Receiver:
            self.connect(name, func)
            return func
        return decorator

    def emit(self, name: str, *args: Any, **kwargs: Any) -> List[Tuple[Receiver, Any]]:
        """Send the named signal to its receivers."""
        return self.signal(name).send(*args, **kwargs)

    def names(self) -> List[str]:
        """The registered signal names."""
        return list(self._signals.keys())

    def __repr__(self) -> str:
        return "<HookRegistry {}>".format(sorted(self._signals))


#: Module-level lifecycle signals for apps that prefer a shared bus.
before_request = Signal("before_request")
after_request = Signal("after_request")
