import unittest

from taskflow.queue import ReadyQueue


class ReadyQueueTest(unittest.TestCase):
    def test_fifo_tiebreak(self):
        # Equal priority falls back to insertion (first-in-first-out) order.
        q = ReadyQueue()
        q.push("first", priority=5)
        q.push("second", priority=5)
        q.push("third", priority=5)
        self.assertEqual(q.drain(), ["first", "second", "third"])

    def test_priority_order(self):
        # Higher priority is dispatched first regardless of insertion order.
        q = ReadyQueue()
        q.push("low", priority=1)
        q.push("mid", priority=2)
        q.push("high", priority=9)
        self.assertEqual(q.pop(), "high")
        self.assertEqual(q.pop(), "mid")
        self.assertEqual(q.pop(), "low")


if __name__ == "__main__":
    unittest.main()
