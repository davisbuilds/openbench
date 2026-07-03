"""Dependency graph for a pipeline of tasks.

The :class:`Dag` is a directed acyclic graph whose nodes are task identifiers
and whose edges point from a dependency to the task that depends on it -- an
edge ``a -> b`` means "``b`` depends on ``a``", so ``a`` must finish before
``b`` may start.

The class supports the queries the scheduler and reporting layers need:

* :meth:`Dag.topological_levels` -- the graph flattened into ordered "waves"
  of tasks that could run in parallel.
* :meth:`Dag.detect_cycle` / :meth:`Dag.assert_acyclic` -- cycle detection.
* :meth:`Dag.transitive_dependents` -- everything downstream of a node, used to
  cascade skips when a task fails permanently.
* :meth:`Dag.roots` / :meth:`Dag.leaves` -- entry and exit nodes.

All traversals visit neighbours in the deterministic order the nodes and edges
were added, so results are reproducible.
"""

from __future__ import annotations

from taskflow.model import GraphError


class Dag:
    """A directed acyclic graph of task dependencies.

    Internally the graph keeps two adjacency maps: ``_deps`` maps a node to the
    nodes it depends on (its predecessors) and ``_dependents`` maps a node to
    the nodes that depend on it (its successors). Both are kept in sync by
    :meth:`add_edge`. Insertion order is preserved in every list so traversals
    are deterministic.
    """

    def __init__(self):
        self._nodes = []
        self._node_set = set()
        self._deps = {}
        self._dependents = {}

    # -- construction -----------------------------------------------------

    def add_task(self, task_id):
        """Add an isolated node. Idempotent for an already-present node."""

        if task_id in self._node_set:
            return
        self._node_set.add(task_id)
        self._nodes.append(task_id)
        self._deps[task_id] = []
        self._dependents[task_id] = []

    def add_edge(self, dependency, dependent):
        """Record that ``dependent`` depends on ``dependency``.

        Both endpoints are created on demand. Duplicate edges are ignored so
        the adjacency lists never contain repeats. A self-edge is rejected
        immediately because it is a trivial cycle.
        """

        if dependency == dependent:
            raise GraphError(
                "task {!r} cannot depend on itself".format(dependency)
            )
        self.add_task(dependency)
        self.add_task(dependent)
        if dependency not in self._deps[dependent]:
            self._deps[dependent].append(dependency)
        if dependent not in self._dependents[dependency]:
            self._dependents[dependency].append(dependent)

    @classmethod
    def from_pipeline(cls, pipeline):
        """Build a graph from a :class:`~taskflow.model.Pipeline`.

        Every task becomes a node, and every declared dependency becomes an
        edge. A dependency that names a task not present in the pipeline is a
        configuration error surfaced as a :class:`GraphError`.
        """

        dag = cls()
        for task in pipeline.tasks():
            dag.add_task(task.id)
        for task in pipeline.tasks():
            for dep in task.deps:
                if not pipeline.has(dep):
                    raise GraphError(
                        "task {!r} depends on unknown task {!r}".format(
                            task.id, dep
                        )
                    )
                dag.add_edge(dep, task.id)
        return dag

    # -- basic queries ----------------------------------------------------

    def nodes(self):
        """Return all node identifiers in insertion order."""

        return list(self._nodes)

    def __len__(self):
        return len(self._nodes)

    def __contains__(self, task_id):
        return task_id in self._node_set

    def dependencies(self, task_id):
        """Return the direct dependencies (predecessors) of ``task_id``."""

        self._require(task_id)
        return list(self._deps[task_id])

    def dependents(self, task_id):
        """Return the direct dependents (successors) of ``task_id``."""

        self._require(task_id)
        return list(self._dependents[task_id])

    def roots(self):
        """Return nodes with no dependencies, in insertion order."""

        return [n for n in self._nodes if not self._deps[n]]

    def leaves(self):
        """Return nodes with no dependents, in insertion order."""

        return [n for n in self._nodes if not self._dependents[n]]

    def in_degree(self, task_id):
        """Return the number of direct dependencies of ``task_id``."""

        self._require(task_id)
        return len(self._deps[task_id])

    def out_degree(self, task_id):
        """Return the number of direct dependents of ``task_id``."""

        self._require(task_id)
        return len(self._dependents[task_id])

    # -- transitive queries ----------------------------------------------

    def transitive_dependents(self, task_id):
        """Return every node reachable downstream of ``task_id``.

        The traversal is a breadth-first walk over the ``_dependents`` map, so
        the returned list is ordered by distance from ``task_id`` and, within a
        distance, by insertion order. ``task_id`` itself is never included.
        This is exactly the set of tasks that must be skipped when ``task_id``
        fails permanently.
        """

        self._require(task_id)
        seen = set()
        ordered = []
        frontier = list(self._dependents[task_id])
        while frontier:
            node = frontier.pop(0)
            if node in seen:
                continue
            seen.add(node)
            ordered.append(node)
            for nxt in self._dependents[node]:
                if nxt not in seen:
                    frontier.append(nxt)
        return ordered

    def transitive_dependencies(self, task_id):
        """Return every node reachable upstream of ``task_id`` (its ancestors)."""

        self._require(task_id)
        seen = set()
        ordered = []
        frontier = list(self._deps[task_id])
        while frontier:
            node = frontier.pop(0)
            if node in seen:
                continue
            seen.add(node)
            ordered.append(node)
            for nxt in self._deps[node]:
                if nxt not in seen:
                    frontier.append(nxt)
        return ordered

    # -- ordering ---------------------------------------------------------

    def topological_levels(self):
        """Return the graph as an ordered list of parallel "levels".

        Level 0 contains the roots; level ``k`` contains every node whose
        dependencies are all satisfied by levels ``< k`` and that is not itself
        placeable earlier. This is a deterministic Kahn's-algorithm layering:
        nodes are emitted in insertion order within each level. Raises
        :class:`GraphError` if the graph contains a cycle (some nodes would
        never become placeable).
        """

        indegree = {n: len(self._deps[n]) for n in self._nodes}
        placed = set()
        levels = []
        remaining = set(self._nodes)

        while remaining:
            current = [
                n for n in self._nodes if n in remaining and indegree[n] == 0
            ]
            if not current:
                raise GraphError(
                    "cycle detected; cannot compute topological levels"
                )
            levels.append(current)
            for node in current:
                placed.add(node)
                remaining.discard(node)
                for dependent in self._dependents[node]:
                    indegree[dependent] -= 1
        return levels

    def topological_order(self):
        """Return a single flat topological ordering of all nodes."""

        order = []
        for level in self.topological_levels():
            order.extend(level)
        return order

    # -- cycle detection --------------------------------------------------

    def detect_cycle(self):
        """Return a list of nodes forming a cycle, or ``[]`` if acyclic.

        Uses an iterative depth-first search with a three-colour marking
        (white/grey/black) so it works on large graphs without recursion
        limits. When a back-edge to a grey node is found, the current DFS stack
        is sliced to return the participating nodes in order.
        """

        WHITE, GREY, BLACK = 0, 1, 2
        colour = {n: WHITE for n in self._nodes}

        for start in self._nodes:
            if colour[start] != WHITE:
                continue
            # Each stack frame: (node, iterator over its dependents).
            stack = [(start, iter(self._dependents[start]))]
            path = [start]
            colour[start] = GREY
            while stack:
                node, it = stack[-1]
                advanced = False
                for nxt in it:
                    if colour[nxt] == GREY:
                        idx = path.index(nxt)
                        return path[idx:] + [nxt]
                    if colour[nxt] == WHITE:
                        colour[nxt] = GREY
                        stack.append((nxt, iter(self._dependents[nxt])))
                        path.append(nxt)
                        advanced = True
                        break
                if not advanced:
                    colour[node] = BLACK
                    stack.pop()
                    path.pop()
        return []

    def has_cycle(self):
        """Return ``True`` if the graph contains at least one cycle."""

        return bool(self.detect_cycle())

    def assert_acyclic(self):
        """Raise :class:`GraphError` naming the cycle if one exists."""

        cycle = self.detect_cycle()
        if cycle:
            raise GraphError(
                "dependency cycle detected: {}".format(" -> ".join(cycle))
            )

    # -- reachability -----------------------------------------------------

    def is_reachable(self, source, target):
        """Return ``True`` if ``target`` lies downstream of ``source``.

        A node is not considered to reach itself unless a genuine directed
        path returns to it (which, in an acyclic graph, never happens). The
        check is a breadth-first walk over the dependents map and stops as soon
        as ``target`` is found.
        """

        self._require(source)
        self._require(target)
        seen = set()
        frontier = list(self._dependents[source])
        while frontier:
            node = frontier.pop(0)
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(self._dependents[node])
        return False

    def ancestors(self, task_id):
        """Return the set of nodes ``task_id`` transitively depends on."""

        return set(self.transitive_dependencies(task_id))

    def descendants(self, task_id):
        """Return the set of nodes that transitively depend on ``task_id``."""

        return set(self.transitive_dependents(task_id))

    def is_independent(self, a, b):
        """Return ``True`` if neither of ``a`` and ``b`` can reach the other.

        Two independent nodes may safely run in parallel: no dependency path
        connects them in either direction.
        """

        return not (self.is_reachable(a, b) or self.is_reachable(b, a))

    # -- shape metrics ----------------------------------------------------

    def depth(self):
        """Return the number of topological levels (the graph's critical depth).

        An empty graph has depth 0; a graph of isolated nodes has depth 1.
        """

        if not self._nodes:
            return 0
        return len(self.topological_levels())

    def width(self):
        """Return the size of the largest topological level (max parallelism)."""

        levels = self.topological_levels() if self._nodes else []
        return max((len(level) for level in levels), default=0)

    def level_of(self, task_id):
        """Return the zero-based topological level ``task_id`` sits in."""

        self._require(task_id)
        for index, level in enumerate(self.topological_levels()):
            if task_id in level:
                return index
        raise GraphError("unreachable: node not placed in any level")

    def longest_path_length(self):
        """Return the length (in edges) of the longest dependency chain.

        This is the critical-path length: the number of edges on the longest
        root-to-leaf path. A single node has length 0. Computed by dynamic
        programming over a topological order, so it is linear in the graph size
        and raises on a cyclic graph.
        """

        order = self.topological_order()
        longest = {node: 0 for node in self._nodes}
        best = 0
        for node in order:
            for dependent in self._dependents[node]:
                candidate = longest[node] + 1
                if candidate > longest[dependent]:
                    longest[dependent] = candidate
                    if candidate > best:
                        best = candidate
        return best

    def edges(self):
        """Return every ``(dependency, dependent)`` edge in a stable order."""

        pairs = []
        for node in self._nodes:
            for dependent in self._dependents[node]:
                pairs.append((node, dependent))
        return pairs

    def edge_count(self):
        """Return the total number of directed edges in the graph."""

        return sum(len(v) for v in self._dependents.values())

    def reverse(self):
        """Return a new :class:`Dag` with every edge direction flipped.

        The reversed graph has the same nodes; a dependency becomes a dependent
        and vice versa. Useful for running downstream analyses (such as
        ancestor queries) as if they were dependent queries.
        """

        flipped = Dag()
        for node in self._nodes:
            flipped.add_task(node)
        for dependency, dependent in self.edges():
            flipped.add_edge(dependent, dependency)
        return flipped

    def subgraph(self, task_ids):
        """Return the induced subgraph over ``task_ids``.

        Only edges whose *both* endpoints are in ``task_ids`` are carried over.
        Every requested node must exist in this graph.
        """

        wanted = list(task_ids)
        wanted_set = set(wanted)
        for node in wanted:
            self._require(node)
        sub = Dag()
        for node in self._nodes:
            if node in wanted_set:
                sub.add_task(node)
        for dependency, dependent in self.edges():
            if dependency in wanted_set and dependent in wanted_set:
                sub.add_edge(dependency, dependent)
        return sub

    def describe(self):
        """Return a compact multi-line human-readable description of the graph."""

        lines = [
            "Dag: {} nodes, {} edges, depth {}, width {}".format(
                len(self._nodes), self.edge_count(), self.depth(), self.width()
            ),
            "  roots: {}".format(", ".join(self.roots()) or "(none)"),
            "  leaves: {}".format(", ".join(self.leaves()) or "(none)"),
        ]
        return "\n".join(lines)

    # -- internals --------------------------------------------------------

    def _require(self, task_id):
        if task_id not in self._node_set:
            raise GraphError("unknown task: {!r}".format(task_id))

    def __repr__(self):
        return "Dag(nodes={}, edges={})".format(
            len(self._nodes),
            sum(len(v) for v in self._dependents.values()),
        )
