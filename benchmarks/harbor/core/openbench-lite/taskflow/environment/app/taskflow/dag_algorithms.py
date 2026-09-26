"""Advanced graph analyses layered on top of :class:`taskflow.dag.Dag`.

:class:`~taskflow.dag.Dag` deliberately keeps a compact core API -- adjacency,
levels, cycle detection, a handful of shape metrics. This module adds the
heavier, more specialised analyses that planning, reporting and diagnostics
want, *without* touching ``dag.py`` itself: every function here takes a ``Dag``
and reads it through its public interface only.

What lives here:

* :func:`weighted_longest_path` -- the critical path when nodes carry a cost
  (e.g. a task's duration), returning both the path and its total weight.
* :func:`level_widths` / :func:`topological_generations` -- the shape of the
  parallel-wave decomposition.
* :func:`ancestor_closure` / :func:`descendant_closure` -- reachable sets with
  the node itself optionally included.
* :func:`is_tree`, :func:`connected_components`, :func:`branching_factor` --
  structural classifiers.
* :func:`all_paths` -- enumerate every root-to-leaf or ``a``-to-``b`` path.

The traversals mirror the deterministic ordering of the underlying ``Dag`` --
neighbours are visited in the insertion order the graph preserves -- so results
are reproducible across runs.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

from taskflow.dag import Dag
from taskflow.model import GraphError


def weighted_longest_path(
    dag: Dag, weights: Optional[Dict[str, int]] = None, default_weight: int = 1
) -> Tuple[List[str], int]:
    """Return the maximum-weight path through ``dag`` and its total weight.

    Each node contributes ``weights[node]`` (or ``default_weight`` if absent) to
    the weight of any path it lies on. With the default uniform weight of 1 the
    result's weight is the number of nodes on the longest chain -- the critical
    *depth* in tasks. Passing task durations as weights turns it into the
    critical *path* in virtual ticks: a lower bound on any schedule's makespan.

    Returns a ``(path, weight)`` tuple; the path is a list of node ids from an
    upstream root to a downstream leaf. Raises :class:`GraphError` on a cyclic
    graph (via the underlying topological order). An empty graph yields
    ``([], 0)``.
    """

    order = dag.topological_order()
    if not order:
        return [], 0

    def weight_of(node: str) -> int:
        if weights is None:
            return default_weight
        return weights.get(node, default_weight)

    best_weight: Dict[str, int] = {}
    predecessor: Dict[str, Optional[str]] = {}
    for node in order:
        w = weight_of(node)
        deps = dag.dependencies(node)
        if not deps:
            best_weight[node] = w
            predecessor[node] = None
            continue
        best_prev = None
        best_prev_weight = -1
        for dep in deps:
            if best_weight[dep] > best_prev_weight:
                best_prev_weight = best_weight[dep]
                best_prev = dep
        best_weight[node] = best_prev_weight + w
        predecessor[node] = best_prev

    end = max(order, key=lambda n: best_weight[n])
    path: List[str] = []
    cursor: Optional[str] = end
    while cursor is not None:
        path.append(cursor)
        cursor = predecessor[cursor]
    path.reverse()
    return path, best_weight[end]


def critical_path(dag: Dag, durations: Dict[str, int]) -> Tuple[List[str], int]:
    """Return the duration-weighted critical path and its length in ticks.

    A thin, intention-revealing wrapper over :func:`weighted_longest_path` that
    names its weights as task durations. The returned length is the minimum
    number of virtual ticks the pipeline needs even with unlimited concurrency.
    """

    return weighted_longest_path(dag, weights=durations, default_weight=1)


def level_widths(dag: Dag) -> List[int]:
    """Return the number of nodes in each topological level, in order.

    The maximum of this list is the graph's peak achievable parallelism; the
    length is its depth. Handy for a bar-chart-style view of how a pipeline
    fans out and back in over time.
    """

    return [len(level) for level in dag.topological_levels()]


def topological_generations(dag: Dag) -> List[List[str]]:
    """Return the topological levels as lists of node ids (Kahn generations).

    This is the same decomposition :meth:`taskflow.dag.Dag.topological_levels`
    produces; it is re-exposed here under the graph-theory name "generations"
    and validated to be a proper cover so downstream analytics can rely on it.
    """

    levels = dag.topological_levels()
    covered = sum(len(level) for level in levels)
    if covered != len(dag.nodes()):
        raise GraphError("generation cover is incomplete; graph may be cyclic")
    return levels


def ancestor_closure(
    dag: Dag, node: str, include_self: bool = False
) -> Set[str]:
    """Return every node ``node`` transitively depends on.

    With ``include_self`` the node itself is added to the set, which is
    convenient when computing "everything that must have finished for this node
    to be reachable, counting the node".
    """

    result = set(dag.transitive_dependencies(node))
    if include_self:
        result.add(node)
    return result


def descendant_closure(
    dag: Dag, node: str, include_self: bool = False
) -> Set[str]:
    """Return every node that transitively depends on ``node``.

    This is exactly the set the scheduler skips when ``node`` fails permanently.
    ``include_self`` adds the node itself, giving the full "blast radius" of a
    failure including the originating task.
    """

    result = set(dag.transitive_dependents(node))
    if include_self:
        result.add(node)
    return result


def is_tree(dag: Dag) -> bool:
    """Return ``True`` if the graph is a forest of in-trees.

    In dependency terms that means no task has two or more direct dependencies:
    every node has in-degree at most one. Such pipelines have a particularly
    simple structure (each task waits on at most one upstream task), which some
    reporting shortcuts exploit.
    """

    return all(dag.in_degree(node) <= 1 for node in dag.nodes())


def branching_factor(dag: Dag) -> float:
    """Return the mean out-degree across all nodes (fan-out per task).

    A figure near zero means a mostly-linear pipeline; a large figure means each
    task unlocks many downstream tasks. Returns ``0.0`` for an empty graph.
    """

    nodes = dag.nodes()
    if not nodes:
        return 0.0
    return dag.edge_count() / len(nodes)


def connected_components(dag: Dag) -> List[List[str]]:
    """Partition the nodes into weakly-connected components.

    Two nodes are in the same component if a path connects them ignoring edge
    direction. A pipeline often decomposes into several independent sub-graphs
    that could be scheduled or reported on separately; this finds them. Each
    component is returned as a list in the graph's insertion order, and the
    components themselves are ordered by the first node they contain.
    """

    adjacency: Dict[str, Set[str]] = {node: set() for node in dag.nodes()}
    for dependency, dependent in dag.edges():
        adjacency[dependency].add(dependent)
        adjacency[dependent].add(dependency)

    seen: Set[str] = set()
    components: List[List[str]] = []
    for start in dag.nodes():
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: Set[str] = {start}
        while stack:
            node = stack.pop()
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    members.add(neighbour)
                    stack.append(neighbour)
        ordered = [n for n in dag.nodes() if n in members]
        components.append(ordered)
    return components


def is_connected(dag: Dag) -> bool:
    """Return ``True`` if the graph forms a single weakly-connected component."""

    return len(connected_components(dag)) <= 1


def all_paths(
    dag: Dag, source: Optional[str] = None, target: Optional[str] = None
) -> List[List[str]]:
    """Enumerate simple directed paths through the graph.

    With both ``source`` and ``target`` given, returns every path from source to
    target. With only ``source``, returns every path from it to a leaf. With
    neither, returns every root-to-leaf path in the whole graph. Because the
    graph is acyclic every path is simple, so the enumeration terminates.

    Beware that the number of root-to-leaf paths can be exponential in a
    densely connected graph; this is intended for the small pipelines the engine
    typically runs, and for diagnostics rather than hot-loop use.
    """

    sources = [source] if source is not None else dag.roots()
    for s in sources:
        if s not in dag:
            raise GraphError("unknown source node: {!r}".format(s))
    if target is not None and target not in dag:
        raise GraphError("unknown target node: {!r}".format(target))

    paths: List[List[str]] = []

    def walk(node: str, trail: List[str]) -> None:
        trail = trail + [node]
        dependents = dag.dependents(node)
        if target is not None:
            if node == target:
                paths.append(trail)
                return
            for nxt in dependents:
                walk(nxt, trail)
        else:
            if not dependents:
                paths.append(trail)
                return
            for nxt in dependents:
                walk(nxt, trail)

    for s in sources:
        walk(s, [])
    return paths


def longest_chain_through(dag: Dag, node: str) -> List[str]:
    """Return the longest simple path that passes through ``node``.

    Combines the longest upstream chain ending at ``node`` with the longest
    downstream chain starting at it, giving the critical path the node sits on.
    Uses uniform node weights (a chain length in tasks).
    """

    if node not in dag:
        raise GraphError("unknown node: {!r}".format(node))

    # Longest path ending at each node (over ancestors).
    order = dag.topological_order()
    up_len: Dict[str, int] = {}
    up_pred: Dict[str, Optional[str]] = {}
    for n in order:
        best = 0
        pred: Optional[str] = None
        for dep in dag.dependencies(n):
            if up_len[dep] + 1 > best:
                best = up_len[dep] + 1
                pred = dep
        up_len[n] = best
        up_pred[n] = pred

    # Longest path starting at each node (over descendants).
    down_len: Dict[str, int] = {}
    down_succ: Dict[str, Optional[str]] = {}
    for n in reversed(order):
        best = 0
        succ: Optional[str] = None
        for dep in dag.dependents(n):
            if down_len[dep] + 1 > best:
                best = down_len[dep] + 1
                succ = dep
        down_len[n] = best
        down_succ[n] = succ

    upstream: List[str] = []
    cursor: Optional[str] = up_pred[node]
    while cursor is not None:
        upstream.append(cursor)
        cursor = up_pred[cursor]
    upstream.reverse()

    downstream: List[str] = []
    cursor = down_succ[node]
    while cursor is not None:
        downstream.append(cursor)
        cursor = down_succ[cursor]

    return upstream + [node] + downstream


def bottlenecks(dag: Dag) -> List[str]:
    """Return nodes every root-to-leaf path must pass through (articulation-ish).

    A "bottleneck" here is a node present on *every* root-to-leaf path: if it
    fails, the whole pipeline is doomed, so it is worth flagging in a plan. For
    the modest graphs this engine runs we compute it directly by enumerating
    paths and intersecting their node sets.
    """

    paths = all_paths(dag)
    if not paths:
        return []
    common: Set[str] = set(paths[0])
    for path in paths[1:]:
        common &= set(path)
    return [n for n in dag.nodes() if n in common]


def summary(dag: Dag) -> Dict[str, object]:
    """Return a compact dict of structural metrics for reporting.

    Bundles the cheap-to-compute shape figures a plan or dashboard wants in one
    call: node/edge counts, depth, width, branching factor, whether it is a
    tree, and the number of weakly-connected components.
    """

    return {
        "nodes": len(dag.nodes()),
        "edges": dag.edge_count(),
        "depth": dag.depth(),
        "width": dag.width(),
        "roots": dag.roots(),
        "leaves": dag.leaves(),
        "branching_factor": branching_factor(dag),
        "is_tree": is_tree(dag),
        "components": len(connected_components(dag)),
    }
