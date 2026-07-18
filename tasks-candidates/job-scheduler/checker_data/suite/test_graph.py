import unittest

from scheduler.graph import Graph


class GraphTest(unittest.TestCase):
    def test_topo_linear_chain(self):
        # a -> b -> c must come out in dependency order.
        g = Graph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        self.assertEqual(g.topological_order(), ["a", "b", "c"])

    def test_topo_respects_priority(self):
        # "a" and "b" are both ready from the start (no deps). "b" has the higher
        # priority, so it must be emitted before "a" even though "a" sorts first
        # alphabetically. "c" depends on both and comes last.
        g = Graph()
        for n in ("a", "b", "c"):
            g.add_node(n)
        g.add_edge("a", "c")
        g.add_edge("b", "c")
        order = g.topological_order(priority={"a": 1, "b": 5})
        self.assertEqual(order, ["b", "a", "c"])

    def test_transitive_dependents(self):
        # Everything downstream of "root" should be reported.
        g = Graph()
        g.add_edge("root", "mid1")
        g.add_edge("root", "mid2")
        g.add_edge("mid1", "leaf")
        self.assertEqual(set(g.transitive_dependents("root")),
                         {"mid1", "mid2", "leaf"})
        self.assertEqual(g.transitive_dependents("leaf"), [])

    def test_detects_simple_cycle(self):
        g = Graph()
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        self.assertTrue(g.has_cycle())

    def test_detects_self_dependency(self):
        # A job that depends on itself is a (degenerate) cycle.
        g = Graph()
        g.add_edge("x", "x")
        self.assertTrue(g.has_cycle())


if __name__ == "__main__":
    unittest.main()
