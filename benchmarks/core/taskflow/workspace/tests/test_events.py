import unittest

from taskflow.events import EventBus


class EventBusTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.seen = []

    def _record(self, event):
        self.seen.append((event.topic, event.payload.get("n")))

    def test_exact_delivery(self):
        self.bus.subscribe("job.started", self._record)
        delivered = self.bus.publish("job.started", {"n": 1}, at=0)
        self.assertEqual(delivered, 1)
        self.assertEqual(self.seen, [("job.started", 1)])


if __name__ == "__main__":
    unittest.main()
