"""rank_frontier: which explored-but-unexpanded node is worth expanding next (plan §5).

Pure function over the explored graph. Each candidate belongs to the side (source=0, target=1)
from which it is cheapest to reach. Score (lower is better):

    weighted path cost from that side's endpoint (same edge costs as the strongest path)
    + frontier.degree_weight * ln(1 + degree)        # prefer cheap, specific expansions

Excluded: already-expanded nodes, hubs that may not be intermediate nodes (they can never extend
a path), nodes already at the hop limit, and nodes whose edges are all already explored (the
explored graph holds as many of their edges as their full-graph degree, e.g. leaf relatives), since
expanding them cannot add anything. Ties break by hop count, then QID number.
"""

from __future__ import annotations

import heapq
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..config import FrontierPolicy, RelationshipPolicy
from .model import Edge
from .store import GraphView


class ExpansionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)
    qid: str
    side: Literal[0, 1]
    hops: int
    path_cost: float
    degree: int
    score: float
    via_edge_id: str | None  # last edge on the cheapest explored route to this node
    reason: str


def _qkey(qid: str) -> int:
    return int(qid[1:])


def rank_frontier(
    graph: GraphView,
    source_qid: str,
    target_qid: str,
    visited: set[str] | frozenset[str],
    policy: RelationshipPolicy,
    frontier: FrontierPolicy,
    groups: set[str] | frozenset[str] | None = None,
) -> list[ExpansionCandidate]:
    pids = policy.active_pids(groups)
    cap = policy.pathfinding.max_intermediate_degree
    max_hops = policy.pathfinding.max_hops
    endpoints = {source_qid, target_qid}

    def blocked(qid: str) -> bool:
        if qid in endpoints or cap is None:
            return False
        node = graph.node(qid)
        return node is not None and node.kind != "person" and node.degree > cap

    def step(edge: Edge, into: str) -> float:
        c = policy.strength_cost(edge.pid) + policy.ranking.hop_penalty
        node = graph.node(into)
        if into not in endpoints and node is not None and node.kind != "person":
            c += policy.hub_penalty(node.degree)
        return c

    best: dict[str, tuple[float, int, int, str | None]] = {}  # qid -> (cost, hops, side, via edge)
    for side, start in enumerate((source_qid, target_qid)):
        other_end = target_qid if side == 0 else source_qid
        dist: dict[str, tuple[float, int]] = {start: (0.0, 0)}
        via: dict[str, str | None] = {start: None}
        heap: list[tuple[float, int, int, str]] = [(0.0, 0, _qkey(start), start)]
        while heap:
            cost, hops, _, v = heapq.heappop(heap)
            if dist.get(v, (math.inf, 0)) < (cost, hops):
                continue
            prev = best.get(v)
            if prev is None or (cost, hops, side) < prev[:3]:
                best[v] = (cost, hops, side, via[v])
            # Only an expanded node's edges are known; blocked hubs and the opposite endpoint stop routes.
            if v not in visited or hops >= max_hops or (v != start and (blocked(v) or v == other_end)):
                continue
            for e in graph.incident(v, pids):
                w = e.other(v)
                cand = (cost + step(e, w), hops + 1)
                if cand < dist.get(w, (math.inf, 0)):
                    dist[w] = cand
                    via[w] = e.edge_id
                    heapq.heappush(heap, (cand[0], cand[1], _qkey(w), w))

    all_pids = frozenset(policy.properties)
    out = []
    for qid, (cost, hops, side, via_edge) in best.items():
        if qid in visited or blocked(qid) or hops >= max_hops:
            continue
        node = graph.node(qid)
        degree = node.degree if node else 0
        if qid not in endpoints and len(graph.incident(qid, all_pids)) >= degree:
            continue  # nothing left to discover
        score = cost + frontier.degree_weight * math.log1p(degree)
        label = node.label() if node else qid
        end = "source" if side == 0 else "target"
        reason = (f"{label}: {hops} hop{'s' if hops != 1 else ''} from the {end}, "
                  f"route cost {cost:.2f}, degree {degree}")
        out.append(ExpansionCandidate(qid=qid, side=side, hops=hops, path_cost=cost, degree=degree,  # type: ignore[arg-type]
                                      score=score, via_edge_id=via_edge, reason=reason))
    out.sort(key=lambda c: (c.score, c.hops, _qkey(c.qid)))
    return out
