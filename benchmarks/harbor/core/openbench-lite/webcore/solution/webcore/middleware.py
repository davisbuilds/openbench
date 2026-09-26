"""webcore.middleware -- the onion-model middleware chain.

A *middleware* is any callable ``middleware(request, next) -> response`` where
``next`` is a zero-history callable ``next(request) -> response`` representing
"the rest of the stack" (the inner middlewares and, finally, the handler).

The onion model: given middlewares ``[A, B]`` and a handler ``H``,
:func:`build_chain` produces a callable equivalent to::

    A(request, lambda r: B(r, lambda r2: H(r2)))

so on the way *in* the order is A, B, H, and on the way *out* it unwinds H, B,
A. A middleware that returns a response **without** calling ``next`` short-
circuits: every inner layer and the handler are skipped (clause 6).

When an application mounts a sub-application, the parent's middlewares sit
*outside* the child's in this same list, so the parent runs first on the way in
and last on the way out -- exactly the nesting clause 6 describes.
"""


def build_chain(middlewares, handler):
    """Compose ``middlewares`` (outermost first) around ``handler``.

    Returns a callable ``chain(request) -> response``. ``handler`` is the
    innermost callable and takes only the request. Each middleware is passed
    ``(request, next)``.
    """
    chain = handler
    # Wrap from the inside out so the first middleware ends up outermost.
    for middleware in reversed(list(middlewares)):
        chain = _wrap(middleware, chain)
    return chain


def _wrap(middleware, next_callable):
    def invoke(request):
        return middleware(request, next_callable)

    return invoke


class Middleware:
    """Optional base class for class-style middleware.

    Subclasses override :meth:`process` with the same
    ``(request, next) -> response`` contract; instances are directly callable,
    so they slot into :func:`build_chain` like a plain function.
    """

    def process(self, request, next_call):  # pragma: no cover - abstract
        raise NotImplementedError

    def __call__(self, request, next_call):
        return self.process(request, next_call)
