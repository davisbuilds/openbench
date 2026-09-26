"""A small synchronous publish/subscribe event bus.

The scheduler narrates everything it does as a stream of :class:`~taskflow.model.Event`
objects -- jobs starting, succeeding, failing, being skipped, resources being
acquired and released. Observers (most importantly :class:`taskflow.history.History`)
subscribe to those events and build up whatever view they need.

Two kinds of subscription are supported:

* **Exact** -- :meth:`EventBus.subscribe` matches one topic string exactly.
* **Prefix** -- :meth:`EventBus.subscribe_prefix` matches a whole dotted
  subtree. Subscribing to the prefix ``"job.term"`` receives ``"job.term"``
  itself and every ``"job.term.<something>"`` such as ``"job.term.failed"`` and
  ``"job.term.skipped"``, but not the unrelated ``"job.started"``.

Delivery is synchronous, in-process, and ordered: every published event gets a
monotonically increasing sequence number, and handlers for a topic fire in the
order they subscribed. There are no threads and no real time.
"""

from __future__ import annotations

from taskflow.model import Event


def _prefix_matches(prefix, topic):
    """Return ``True`` if ``topic`` lies in the dotted subtree of ``prefix``.

    A topic matches a prefix when it equals the prefix exactly or when it is a
    dotted descendant of it. The trailing dot is required so that the prefix
    ``"job"`` matches ``"job.started"`` but not an unrelated ``"jobless"``.
    """

    return topic == prefix or prefix.startswith(topic + ".")


class Subscription:
    """A handle returned by ``subscribe`` that can later cancel delivery."""

    __slots__ = ("bus", "kind", "key", "handler", "active")

    def __init__(self, bus, kind, key, handler):
        self.bus = bus
        self.kind = kind  # "exact" or "prefix"
        self.key = key
        self.handler = handler
        self.active = True

    def cancel(self):
        """Stop delivering events to this subscription. Idempotent."""

        if self.active:
            self.bus._remove(self)
            self.active = False

    def __repr__(self):
        return "Subscription(kind={!r}, key={!r}, active={})".format(
            self.kind, self.key, self.active
        )


class EventBus:
    """A synchronous, deterministic publish/subscribe bus.

    Exact subscribers are stored per topic; prefix subscribers are stored in a
    flat, insertion-ordered list and consulted on every publish. Each publish
    stamps the event with the next sequence number and the supplied virtual
    time, then delivers it -- first to the exact subscribers of its topic, then
    to every matching prefix subscriber -- in subscription order.
    """

    def __init__(self):
        self._exact = {}
        self._prefix = []
        self._seq = 0
        self._published = []

    # -- subscription -----------------------------------------------------

    def subscribe(self, topic, handler):
        """Register ``handler`` for events published on exactly ``topic``.

        Returns a :class:`Subscription`. The same handler may subscribe to
        several topics; each call yields its own cancellable handle.
        """

        sub = Subscription(self, "exact", topic, handler)
        self._exact.setdefault(topic, []).append(sub)
        return sub

    def subscribe_prefix(self, prefix, handler):
        """Register ``handler`` for every event whose topic is under ``prefix``.

        Returns a :class:`Subscription`. Prefix subscribers see the whole
        dotted subtree rooted at ``prefix`` (see :func:`_prefix_matches`).
        """

        sub = Subscription(self, "prefix", prefix, handler)
        self._prefix.append(sub)
        return sub

    def _remove(self, sub):
        """Detach a subscription (called by :meth:`Subscription.cancel`)."""

        if sub.kind == "exact":
            bucket = self._exact.get(sub.key)
            if bucket and sub in bucket:
                bucket.remove(sub)
                if not bucket:
                    del self._exact[sub.key]
        else:
            if sub in self._prefix:
                self._prefix.remove(sub)

    # -- publishing -------------------------------------------------------

    def publish(self, topic, payload=None, at=0):
        """Publish an event on ``topic`` and deliver it synchronously.

        A fresh :class:`~taskflow.model.Event` is created, stamped with the
        next sequence number and the virtual time ``at``, appended to the bus's
        own record, and handed to every matching handler. Returns the number of
        handlers the event was delivered to.
        """

        event = Event(topic=topic, payload=payload, seq=self._seq, at=at)
        self._seq += 1
        self._published.append(event)

        delivered = 0
        # Exact subscribers of this precise topic, in subscription order.
        for sub in list(self._exact.get(topic, [])):
            if sub.active:
                sub.handler(event)
                delivered += 1
        # Prefix subscribers whose subtree contains this topic.
        for sub in list(self._prefix):
            if sub.active and _prefix_matches(sub.key, topic):
                sub.handler(event)
                delivered += 1
        return delivered

    # -- introspection ----------------------------------------------------

    def history(self):
        """Return every event ever published, in publication order."""

        return list(self._published)

    def topics(self):
        """Return the set of distinct topics that have been published."""

        return {event.topic for event in self._published}

    def subscriber_count(self, topic=None):
        """Return how many handlers would receive an event on ``topic``.

        With ``topic=None`` return the total number of live subscriptions
        (exact plus prefix).
        """

        if topic is None:
            exact = sum(len(bucket) for bucket in self._exact.values())
            return exact + len(self._prefix)
        count = len(self._exact.get(topic, []))
        count += sum(1 for s in self._prefix if _prefix_matches(s.key, topic))
        return count

    def __repr__(self):
        return "EventBus(published={}, exact_topics={}, prefix_subs={})".format(
            len(self._published), len(self._exact), len(self._prefix)
        )
