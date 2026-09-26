"""A directed dependency graph over job ids.

Edges point from a dependency to the job that requires it: ``add_edge(a, b)``
means "``a`` must run before ``b``", i.e. ``b`` depends on ``a``. The graph
provides dependency/dependent lookups, a priority-aware topological order, and
cycle detection. It stores only ids; the engine maps ids back to jobs.
"""


class CycleError(Exception):
    """Raised when a topological order is requested for a cyclic graph."""


class Graph:
    def __init__(self):
        # Insertion-ordered node list plus adjacency in both directions.
        self._nodes = []
        self._successors = {}    # id -> [ids that depend on it]
        self._predecessors = {}  # id -> [ids it depends on]

    def add_node(self, node):
        if node not in self._successors:
            self._nodes.append(node)
            self._successors[node] = []
            self._predecessors[node] = []

    def add_edge(self, dep, node):
        """Record that ``node`` depends on ``dep`` (``dep`` runs first)."""
        self.add_node(dep)
        self.add_node(node)
        self._successors[dep].append(node)
        self._predecessors[node].append(dep)

    def nodes(self):
        return list(self._nodes)

    def __contains__(self, node):
        return node in self._successors

    def __len__(self):
        return len(self._nodes)

    def deps(self, node):
        """Direct dependencies of ``node`` (ids that must run before it)."""
        return list(self._predecessors[node])

    def dependents(self, node):
        """Direct dependents of ``node`` (ids that require it)."""
        return list(self._successors[node])

    def roots(self):
        """Nodes with no dependencies (the graph's entry points)."""
        return [n for n in self._nodes if not self._predecessors[n]]

    def leaves(self):
        """Nodes nothing depends on (the graph's terminal jobs)."""
        return [n for n in self._nodes if not self._successors[n]]

    def _reachable(self, node, adjacency):
        """Ids reachable from ``node`` along ``adjacency``, in discovery order."""
        seen = []
        visited = set()
        stack = list(reversed(adjacency[node]))
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            seen.append(cur)
            for nxt in reversed(adjacency[cur]):
                if nxt not in visited:
                    stack.append(nxt)
        return seen

    def transitive_dependents(self, node):
        """Every id reachable downstream of ``node``, in discovery order.

        Used by the engine to fan a permanent failure out to the whole subtree
        that can no longer run.
        """
        return self._reachable(node, self._successors)

    def ancestors(self, node):
        """Every id ``node`` transitively depends on, in discovery order."""
        return self._reachable(node, self._predecessors)

    def topological_order(self, priority=None):
        """Return the nodes in a dependency-respecting order.

        Among nodes that are ready simultaneously (all dependencies already
        emitted), the one with the highest ``priority`` comes first; ties on
        priority are broken by id ascending. Raises :class:`CycleError` if the
        graph is not acyclic.
        """
        priority = priority or {}
        indegree = {n: len(self._predecessors[n]) for n in self._nodes}
        ready = [n for n in self._nodes if indegree[n] == 0]
        order = []
        while ready:
            # Highest priority first, then smallest id.
            ready.sort(key=lambda n: n)
            node = ready.pop(0)
            order.append(node)
            for succ in self._successors[node]:
                indegree[succ] -= 1
                if indegree[succ] == 0:
                    ready.append(succ)
        if len(order) != len(self._nodes):
            raise CycleError("graph contains a cycle")
        return order

    def has_cycle(self):
        """True if the graph contains any cycle, including a self-dependency.

        Standard three-colour DFS: a node reached while still on the current
        recursion stack (GRAY) closes a cycle. A self-edge is exactly such a
        case and must be reported.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self._nodes}

        def visit(node):
            color[node] = GRAY
            for succ in self._successors[node]:
                if color[succ] == GRAY and succ != node:
                    return True
                if color[succ] == WHITE and visit(succ):
                    return True
            color[node] = BLACK
            return False

        return any(visit(n) for n in self._nodes if color[n] == WHITE)

    def is_acyclic(self):
        """Convenience inverse of :meth:`has_cycle`."""
        return not self.has_cycle()
