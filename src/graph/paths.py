"""Deterministic shortest / strongest path search over a ``GraphView``.

Semantics (see IMPLEMENTATION_PLAN.md §5 and config/relationships.yaml):

* Only properties in the enabled relation groups are traversable; every eligible edge can be
  crossed in either direction, and the original Wikidata direction is reported per hop.
* A non-person node whose degree exceeds ``max_intermediate_degree`` may be a path endpoint but
  never an intermediate node (hub policy).
* ``edge_cost = -ln(max(minimum_strength, strength)) + hop_penalty + hub_penalty(entered node)``;
  all terms are >= 0, so Dijkstra is valid. Hub penalty applies only to intermediate non-person
  nodes.
* **Shortest**: fewest eligible edges (bidirectional BFS), ties broken by lower cost, then by the
  sequence of edge IDs.
* **Strongest**: minimum total cost subject to ``max_hops`` (hop-aware Dijkstra over
  (node, hops) states), ties broken by edge-ID sequence. The shortest path's cost is used as an
  upper bound, since the strongest path can never cost more.
* Results carry ``scope``: ``complete_configured_scope`` only when the graph store is complete
  AND the search finished within its work caps; otherwise ``explored_graph``.
"""

from __future__ import annotations

import hashlib
import math
import heapq
import itertools
from dataclasses import dataclass, field
from typing import Literal

from ..config import RelationshipPolicy
from ..models import (
    CostBreakdown,
    PathComparison,
    PathHop,
    PathNode,
    PathOutcome,
    PathResult,
    Scope,
    SearchStats,
)
from .model import Edge, Node, is_qid
from .store import GraphView

_ROUND = 9


def _qkey(qid: str) -> int:
    return int(qid[1:])


@dataclass
class _Search:
    graph: GraphView
    policy: RelationshipPolicy
    source: str
    target: str
    groups: frozenset[str]
    pids: frozenset[str] = field(init=False)
    _nodes: dict[str, Node | None] = field(default_factory=dict, init=False)
    _profiles: dict[str, tuple[str, int] | None] = field(default_factory=dict, init=False)
    _incident: dict[str, list[Edge]] = field(default_factory=dict, init=False)
    _entry_costs: dict[tuple[str, str], float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not (is_qid(self.source) and is_qid(self.target)):
            raise ValueError(f"source/target must be QIDs: {self.source!r}, {self.target!r}")
        if self.source == self.target:
            raise ValueError("source and target must differ")
        self.pids = self.policy.active_pids(self.groups)
        self._strength_costs = {pid: self.policy.strength_cost(pid) for pid in self.pids}

    def profile(self, qid: str) -> tuple[str, int] | None:
        """(kind, degree) of a node; uses the store's label-free fast path when it has one."""
        if qid not in self._profiles:
            fast = getattr(self.graph, "node_profile", None)
            if fast is not None:
                self._profiles[qid] = fast(qid)
            else:
                node = self.node(qid)
                self._profiles[qid] = None if node is None else (node.kind, node.degree)
        return self._profiles[qid]

    @property
    def max_hops(self) -> int:
        return self.policy.pathfinding.max_hops

    def node(self, qid: str) -> Node | None:
        if qid not in self._nodes:
            self._nodes[qid] = self.graph.node(qid)
        return self._nodes[qid]

    def is_endpoint(self, qid: str) -> bool:
        return qid == self.source or qid == self.target

    def blocked(self, qid: str) -> bool:
        """True if ``qid`` may not be an intermediate node under the hub policy."""
        if self.is_endpoint(qid):
            return False
        prof = self.profile(qid)
        return prof is not None and self.policy.pathfinding.is_hub(prof[0], prof[1])

    def incident(self, qid: str) -> list[Edge]:
        cached = self._incident.get(qid)
        if cached is None:
            cached = self._incident[qid] = self.graph.incident(qid, self.pids)
        return cached

    def _hub(self, into: str) -> float:
        if self.is_endpoint(into):
            return 0.0
        prof = self.profile(into)
        return self.policy.hub_penalty(prof[1]) if prof is not None and prof[0] != "person" else 0.0

    def step_total(self, edge: Edge, into: str) -> float:
        """``step_cost(edge, into).total`` without building the breakdown (search inner loop)."""
        key = (edge.pid, into)
        total = self._entry_costs.get(key)
        if total is None:
            total = self._entry_costs[key] = (
                self._strength_costs[edge.pid] + self.policy.ranking.hop_penalty + self._hub(into))
        return total

    def step_cost(self, edge: Edge, into: str) -> CostBreakdown:
        prop = self.policy.properties[edge.pid]
        strength_cost = self.policy.strength_cost(edge.pid)
        hop = self.policy.ranking.hop_penalty
        hub = self._hub(into)
        return CostBreakdown(
            strength=prop.strength,
            strength_cost=strength_cost,
            hop_penalty=hop,
            hub_penalty=hub,
            total=strength_cost + hop + hub,
        )

    def scope(self, truncated: bool) -> Scope:
        return "complete_configured_scope" if self.graph.complete and not truncated else "explored_graph"

    # ---- result assembly ---------------------------------------------------------------------

    def build_result(self, mode: Literal["shortest", "strongest"], edges: list[Edge], scope: Scope) -> PathResult:
        node_seq = [self.source]
        hops: list[PathHop] = []
        total = 0.0
        for edge in edges:
            frm = node_seq[-1]
            to = edge.other(frm)
            node_seq.append(to)
            prop = self.policy.properties[edge.pid]
            forward = edge.subject == frm
            cost = self.step_cost(edge, to)
            total += cost.total
            hops.append(
                PathHop(
                    edge_id=edge.edge_id,
                    from_qid=frm,
                    to_qid=to,
                    subject=edge.subject,
                    pid=edge.pid,
                    object=edge.object,
                    direction="forward" if forward else "reverse",
                    relation_group=prop.group,
                    label=prop.label if forward else prop.inverse_label,
                    label_es=prop.label_es if forward else prop.inverse_label_es,
                    property_url=edge.property_url,
                    source=edge.source,
                    origin=edge.origin,
                    cost=cost,
                )
            )
        if node_seq[-1] != self.target:
            raise AssertionError("path does not end at target")
        nodes = []
        for qid in node_seq:
            n = self.node(qid) or Node(qid=qid)
            nodes.append(PathNode(qid=qid, label_en=n.label_en, label_es=n.label_es, kind=n.kind, url=n.url))
        path_id = "p-" + hashlib.sha1("|".join(e.edge_id for e in edges).encode()).hexdigest()[:12]
        return PathResult(
            path_id=path_id,
            mode=mode,
            source_qid=self.source,
            target_qid=self.target,
            nodes=nodes,
            hops=hops,
            hop_count=len(hops),
            total_cost=round(total, _ROUND),
            scope=scope,
            policy_version=self.policy.version,
            enabled_groups=sorted(self.groups),
            snapshot=self.graph.snapshot,
        )


def _resolve_groups(policy: RelationshipPolicy, groups: set[str] | frozenset[str] | None) -> frozenset[str]:
    return policy.default_groups if groups is None else frozenset(groups)


# ---------------------------------------------------------------------------------------------
# Shortest: bidirectional BFS, then min-cost selection among all shortest paths
# ---------------------------------------------------------------------------------------------


def compute_shortest_path(
    graph: GraphView,
    source_qid: str,
    target_qid: str,
    policy: RelationshipPolicy,
    groups: set[str] | frozenset[str] | None = None,
) -> PathOutcome:
    s = _Search(graph, policy, source_qid, target_qid, _resolve_groups(policy, groups))
    cap = policy.pathfinding.max_visited_nodes

    dist: list[dict[str, int]] = [{s.source: 0}, {s.target: 0}]
    preds: list[dict[str, list[tuple[str, Edge]]]] = [{s.source: []}, {s.target: []}]
    fronts: list[list[str]] = [[s.source], [s.target]]
    depth = [0, 0]
    visited = 2
    expanded = 0
    truncated: str | None = None
    meet: list[str] = []

    while fronts[0] and fronts[1] and depth[0] + depth[1] < s.max_hops:
        side = 0 if len(fronts[0]) <= len(fronts[1]) else 1
        other = 1 - side
        nd = depth[side] + 1
        new: list[str] = []
        for u in fronts[side]:
            if s.blocked(u):
                continue
            expanded += 1
            for edge in s.incident(u):
                v = edge.other(u)
                if s.blocked(v):
                    continue
                d = dist[side].get(v)
                if d is None:
                    dist[side][v] = nd
                    preds[side][v] = [(u, edge)]
                    new.append(v)
                    visited += 1
                elif d == nd:
                    preds[side][v].append((u, edge))
            if visited > cap:
                truncated = f"max_visited_nodes ({cap}) reached"
                break
        depth[side] = nd
        fronts[side] = sorted(new, key=_qkey)
        if truncated:
            break
        meet = [m for m in fronts[side] if m in dist[other]]
        if meet:
            length = min(dist[0][m] + dist[1][m] for m in meet)
            meet = [m for m in meet if dist[0][m] + dist[1][m] == length]
            break

    stats = SearchStats(visited_nodes=visited, expanded_states=expanded, truncated_reason=truncated)
    scope = s.scope(truncated is not None)
    if not meet:
        status = "budget_exhausted" if truncated else "no_path_within_bounds"
        return PathOutcome(mode="shortest", status=status, result=None, scope=scope, stats=stats)

    # Best (cost, edge-id sequence) among shortest-hop half paths on each side.
    memo: dict[tuple[int, str], tuple[float, tuple[str, ...], tuple[Edge, ...]]] = {}

    def best(side: int, v: str) -> tuple[float, tuple[str, ...], tuple[Edge, ...]]:
        key = (side, v)
        if key in memo:
            return memo[key]
        if not preds[side][v]:  # the endpoint itself
            memo[key] = (0.0, (), ())
            return memo[key]
        candidates = []
        for u, edge in preds[side][v]:
            c_u, seq_u, path_u = best(side, u)
            if side == 0:  # source ... u -> v
                cost = c_u + s.step_total(edge, v)
                candidates.append((round(cost, _ROUND), seq_u + (edge.edge_id,), cost, path_u + (edge,)))
            else:  # v -> u ... target
                cost = c_u + s.step_total(edge, u)
                candidates.append((round(cost, _ROUND), (edge.edge_id,) + seq_u, cost, (edge,) + path_u))
        _, seq, cost, path = min(candidates, key=lambda c: (c[0], c[1]))
        memo[key] = (cost, seq, path)
        return memo[key]

    choices = []
    for m in meet:
        c0, q0, p0 = best(0, m)
        c1, q1, p1 = best(1, m)
        choices.append((round(c0 + c1, _ROUND), q0 + q1, p0 + p1))
    _, _, edges = min(choices, key=lambda c: (c[0], c[1]))
    result = s.build_result("shortest", list(edges), scope)
    return PathOutcome(mode="shortest", status="found", result=result, scope=scope, stats=stats)


# ---------------------------------------------------------------------------------------------
# Strongest: hop-aware Dijkstra over (node, hops) states
# ---------------------------------------------------------------------------------------------


def compute_strongest_path(
    graph: GraphView,
    source_qid: str,
    target_qid: str,
    policy: RelationshipPolicy,
    groups: set[str] | frozenset[str] | None = None,
    upper_bound: float | None = None,
) -> PathOutcome:
    """Minimum policy cost path with at most ``max_hops`` edges.

    ``upper_bound`` (e.g. the shortest path's cost) prunes states that cannot beat it; a path of
    exactly that cost is still found.
    """
    s = _Search(graph, policy, source_qid, target_qid, _resolve_groups(policy, groups))
    cap = policy.pathfinding.max_expanded_states
    bound = math.inf if upper_bound is None else round(upper_bound, _ROUND)
    H = s.max_hops

    # Bidirectional, hop-aware Dijkstra with Pareto (hops, cost) labels per node and side.
    # Side 0 grows from the source; side 1 grows from the target. Cost convention: entering a node
    # is charged once — forward labels include the meeting node's hub penalty, backward labels
    # include only nodes strictly between the meeting node and the target.
    counter = itertools.count()
    starts = (s.source, s.target)
    # heap entry: (cost_key, edge-id seq, counter, cost, node, hops, edges, nodes)
    heaps: list[list[tuple]] = [
        [(0.0, (), next(counter), 0.0, s.source, 0, (), (s.source,))],
        [(0.0, (), next(counter), 0.0, s.target, 0, (), (s.target,))],
    ]
    labels: list[dict[str, list[tuple[int, float, tuple[str, ...], tuple[Edge, ...], tuple[str, ...]]]]] = [{}, {}]
    expanded = 0
    truncated: str | None = None
    best: tuple[float, tuple[str, ...], tuple[Edge, ...]] | None = None

    def top(side: int) -> float:
        return heaps[side][0][0] if heaps[side] else math.inf

    def consider(side: int, total: float, seq: tuple, edges: tuple, seq2: tuple, edges2: tuple) -> None:
        """Record a complete path. (seq, edges) belong to ``side``; (seq2, edges2) to the other."""
        nonlocal best
        key = round(total, _ROUND)
        if key > bound:
            return
        cand = (key, seq + seq2, edges + edges2) if side == 0 else (key, seq2 + seq, edges2 + edges)
        if best is None or cand[:2] < best[:2]:
            best = cand

    while heaps[0] or heaps[1]:
        t0, t1 = top(0), top(1)
        if best is not None and t0 + t1 >= best[0]:
            break
        side = 0 if t0 <= t1 else 1
        other = 1 - side
        key, seq, _, cost, v, hops, edges, nodes = heapq.heappop(heaps[side])
        if any(h <= hops for h, *_ in labels[side].get(v, ())):
            continue  # dominated: settled earlier with <= cost and <= hops
        labels[side].setdefault(v, []).append((hops, cost, seq, edges, nodes))
        expanded += 1
        if expanded > cap:
            truncated = f"max_expanded_states ({cap}) reached"
            break

        # Complete a path through v with settled labels of the other side (shared node: v only).
        for h2, c2, seq2, edges2, nodes2 in labels[other].get(v, ()):
            if hops + h2 <= H and len(set(nodes) & set(nodes2)) == 1:
                consider(side, cost + c2, seq, edges, seq2, edges2)

        if hops >= H or v == starts[other] or s.blocked(v):
            continue
        for edge in s.incident(v):
            w = edge.other(v)
            if w in nodes or s.blocked(w):
                continue
            # Forward: entering w. Backward (w -> v -> ... target): entering v.
            step = s.step_total(edge, w if side == 0 else v)
            new_cost = cost + step
            # Complete across this edge with settled labels of the other side at w. Needed for
            # the stopping rule to be exact when the two frontiers are adjacent.
            for h2, c2, seq2, edges2, nodes2 in labels[other].get(w, ()):
                if hops + 1 + h2 <= H and not (set(nodes) & set(nodes2)):
                    if side == 0:
                        consider(0, new_cost + c2, seq + (edge.edge_id,), edges + (edge,), seq2, edges2)
                    else:
                        consider(1, new_cost + c2, (edge.edge_id,) + seq, (edge,) + edges, seq2, edges2)
            new_key = round(new_cost, _ROUND)
            if new_key > bound:
                continue
            if side == 0:
                entry = (new_key, seq + (edge.edge_id,), next(counter), new_cost, w, hops + 1, edges + (edge,), nodes + (w,))
            else:
                entry = (new_key, (edge.edge_id,) + seq, next(counter), new_cost, w, hops + 1, (edge,) + edges, nodes + (w,))
            heapq.heappush(heaps[side], entry)

    visited = len(labels[0].keys() | labels[1].keys())
    stats = SearchStats(visited_nodes=visited, expanded_states=expanded, truncated_reason=truncated)
    scope = s.scope(truncated is not None)
    if best is None:
        status = "budget_exhausted" if truncated else "no_path_within_bounds"
        return PathOutcome(mode="strongest", status=status, result=None, scope=scope, stats=stats)
    # A truncated search may still have found a (possibly non-optimal) path: report it as partial.
    result = s.build_result("strongest", list(best[2]), scope)
    return PathOutcome(mode="strongest", status="budget_exhausted" if truncated else "found",
                       result=result, scope=scope, stats=stats)


# ---------------------------------------------------------------------------------------------


def compare_paths(
    graph: GraphView,
    source_qid: str,
    target_qid: str,
    policy: RelationshipPolicy,
    groups: set[str] | frozenset[str] | None = None,
) -> PathComparison:
    shortest = compute_shortest_path(graph, source_qid, target_qid, policy, groups)
    if shortest.status == "no_path_within_bounds":
        # Same eligible edges, same hop limit: a finished BFS that found nothing proves that no
        # weighted path exists either, so skip the (expensive) weighted search.
        strongest = PathOutcome(
            mode="strongest",
            status="no_path_within_bounds",
            result=None,
            scope=shortest.scope,
            stats=SearchStats(visited_nodes=0, expanded_states=0, truncated_reason=None),
        )
    else:
        bound = shortest.result.total_cost if shortest.result else None
        strongest = compute_strongest_path(graph, source_qid, target_qid, policy, groups, upper_bound=bound)
    same = (
        shortest.result is not None
        and strongest.result is not None
        and shortest.result.path_id == strongest.result.path_id
    )
    scope: Scope = (
        "complete_configured_scope"
        if shortest.scope == strongest.scope == "complete_configured_scope"
        else "explored_graph"
    )
    return PathComparison(
        source_qid=source_qid,
        target_qid=target_qid,
        shortest=shortest,
        strongest=strongest,
        same_path=same,
        scope=scope,
    )
