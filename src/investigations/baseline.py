"""Deterministic bidirectional exploration baseline (plan §6 "fairness" baseline, Milestone 3).

No LLM. Repeatedly expands the best-ranked frontier node (``rank_frontier``), alternating sides
to keep the two searches balanced, applies the provider's batch to the investigation's own
explored graph, recomputes shortest/strongest on that graph, and emits plan §7 events.

Stops when: a path exists and ``expansions_after_first_path`` more expansions are done
("path_found"), the frontier is empty ("frontier_exhausted"), or a budget/cancel fires (partial).
Results are always scoped to the explored graph.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import RelationshipPolicy, SearchConfig
from ..graph.frontier import ExpansionCandidate, rank_frontier
from ..graph.model import Node
from ..graph.paths import compare_paths
from ..models import PathComparison
from ..wikidata.provider import ExpansionBatch, KnowledgeProvider, ProviderError
from .events import edge_payload, node_payload
from .state import ExpansionRecord, InvestigationState

log = logging.getLogger(__name__)

TOOL = "expand_neighbors"


def _budget_stop(state: InvestigationState) -> str | None:
    b = state.budgets
    if state.cancel_event.is_set():
        return "cancelled"
    if state.elapsed() >= b.wall_time_seconds:
        return "wall_time_seconds"
    if state.provider_requests >= b.max_provider_requests:
        return "max_provider_requests"
    if len(state.graph.nodes) >= b.max_explored_nodes:
        return "max_explored_nodes"
    if len(state.graph.edges) >= b.max_explored_edges:
        return "max_explored_edges"
    return None


def _choose(cands: list[ExpansionCandidate], state: InvestigationState) -> ExpansionCandidate:
    """Balance the two searches: take the best candidate of the side with fewer expansions."""
    done = [0, 0]
    for r in state.expansions:
        done[r.side] += 1
    by_side = {s: [c for c in cands if c.side == s] for s in (0, 1)}
    order = sorted((0, 1), key=lambda s: (done[s], s))
    for side in order:
        if by_side[side]:
            return by_side[side][0]
    return cands[0]


def edge_labels(policy: RelationshipPolicy, pid: str) -> dict[str, Any]:
    prop = policy.properties.get(pid)
    if prop is None:
        return {"label": pid, "label_es": pid, "group": None}
    return {"label": prop.label, "label_es": prop.label_es, "group": prop.group}


def apply_batch(state: InvestigationState, batch: ExpansionBatch, policy: RelationshipPolicy
                ) -> tuple[list[dict], list[dict], str | None]:
    """Add a batch to the explored graph within node/edge caps. Returns (nodes, edges, cap_hit)."""
    g, b = state.graph, state.budgets
    records = {n.qid: n.to_node() for n in batch.nodes}
    known = {n.qid for n in g.nodes}
    added_nodes: list[Node] = []
    added_edges = []
    cap_hit = None
    for qid in batch.request.qids:
        if qid not in known:
            g.add_node(records[qid]); known.add(qid); added_nodes.append(records[qid])
    edge_count = len(g.edges)
    for rec in batch.edges:
        edge = rec.to_edge()
        new = [q for q in (edge.subject, edge.object) if q not in known]
        if len(known) + len(new) > b.max_explored_nodes:
            cap_hit = "max_explored_nodes"; break
        if edge_count + 1 > b.max_explored_edges:
            cap_hit = "max_explored_edges"; break
        for q in new:
            g.add_node(records[q]); known.add(q); added_nodes.append(records[q])
        if g.add_edge(edge):
            edge_count += 1
            added_edges.append(edge_payload(edge, **edge_labels(policy, edge.pid)))
    return [node_payload(n) for n in added_nodes], added_edges, cap_hit


def _path_ids(cmp: PathComparison) -> tuple[str | None, str | None]:
    return (cmp.shortest.result.path_id if cmp.shortest.result else None,
            cmp.strongest.result.path_id if cmp.strongest.result else None)


def _found(cmp: PathComparison | None) -> bool:
    return cmp is not None and cmp.shortest.result is not None


def run_deterministic(
    state: InvestigationState,
    provider: KnowledgeProvider,
    policy: RelationshipPolicy,
    search: SearchConfig,
) -> InvestigationState:
    ev = state.events
    s, t = state.source_qid, state.target_qid
    pids = policy.active_pids(state.groups)
    priority = sorted(pids, key=lambda p: (-policy.properties[p].strength, int(p[1:])))
    max_edges = state.budgets.max_edges_per_expansion
    state.status = "running"
    ev.emit("investigation.started", {
        "source_qid": s, "target_qid": t, "strategy": state.strategy, "run_mode": state.run_mode,
        "provider": provider.name, "snapshot": provider.snapshot, "groups": sorted(state.groups),
        "properties": sorted(pids, key=lambda p: int(p[1:])), "policy_version": policy.version,
        "search_config_version": search.version, "budgets": state.budgets.model_dump(),
        "max_hops": policy.pathfinding.max_hops,
        "max_intermediate_degree": policy.pathfinding.max_intermediate_degree,
    })

    def fail(code: str, message: str) -> InvestigationState:
        state.status, state.stop_reason = "failed", code
        ev.emit("investigation.failed", {"error": {"code": code, "message": message}})
        return state

    try:
        if s == t:
            return fail("invalid_request", "source and target must differ")
        found = {r.qid: r for r in provider.get_entities([s, t])}
        for q in (s, t):
            if q not in found:
                return fail("unknown_entity", f"{q} is not in the {provider.name} store")
            if found[q].kind != "person":
                return fail("not_a_person", f"{q} is not recorded as a human (P31 Q5)")
        endpoints = [found[s].to_node(), found[t].to_node()]
        for n in endpoints:
            state.graph.add_node(n)
        ev.emit("identity.resolved", {
            "source": node_payload(endpoints[0]), "target": node_payload(endpoints[1]),
            "added_nodes": [node_payload(n) for n in endpoints], "added_edges": [],
        })

        last_ids: tuple[str | None, str | None] = (None, None)
        expansions_since_found = None
        stop: str | None = None
        step = 0
        while stop is None:
            stop = _budget_stop(state)
            if stop:
                break
            cands = rank_frontier(state.graph, s, t, state.expanded, policy, search.frontier, state.groups)
            if not cands:
                stop = "frontier_exhausted"
                break
            cand = _choose(cands, state)
            step += 1
            node = state.graph.node(cand.qid)
            ev.emit("tool.started", {
                "tool": TOOL, "step": step, "qid": cand.qid, "label": node.label() if node else cand.qid,
                "side": "source" if cand.side == 0 else "target", "reason": cand.reason,
                "score": round(cand.score, 6), "via_edge_id": cand.via_edge_id,
                "arguments": {"qids": [cand.qid], "properties": priority, "direction": "both",
                              "max_edges": max_edges},
            })
            state.provider_requests += 1
            try:
                batch = provider.expand([cand.qid], pids, "both", max_edges, pid_priority=priority)
            except ProviderError as exc:
                state.expansions.append(ExpansionRecord(cand.qid, cand.side, tuple(priority), "both",
                                                        0, False, 0, 0, error=exc.code))
                ev.emit("tool.completed", {"tool": TOOL, "step": step, "qid": cand.qid, "ok": False,
                                           "error": {"code": exc.code, "message": exc.message}})
                continue

            added_nodes, added_edges, cap_hit = apply_batch(state, batch, policy)
            cov = batch.coverage[0]
            complete = cov.complete and cap_hit is None
            state.expansions.append(ExpansionRecord(cand.qid, cand.side, tuple(priority), "both",
                                                    cov.returned, complete, len(added_nodes), len(added_edges)))
            ev.emit("graph.expanded", {
                "step": step, "expanded_qid": cand.qid, "side": "source" if cand.side == 0 else "target",
                "added_nodes": added_nodes, "added_edges": added_edges,
                "highlight_edge_ids": [e["id"] for e in added_edges],
                "returned": cov.returned, "complete": complete, "truncated": not complete,
                "cap_hit": cap_hit, "provider": batch.provider, "snapshot": batch.snapshot,
                "source_urls": batch.source_urls,
            })
            ev.emit("tool.completed", {
                "tool": TOOL, "step": step, "qid": cand.qid, "ok": True, "returned": cov.returned,
                "added_node_count": len(added_nodes), "added_edge_count": len(added_edges), "complete": complete,
            })

            if added_edges:
                cmp = compare_paths(state.graph, s, t, policy, state.groups)
                state.comparison = cmp
                ids = _path_ids(cmp)
                if ids != last_ids:
                    last_ids = ids
                    ev.emit("paths.updated", {"step": step, "trigger_qid": cand.qid,
                                              "comparison": cmp.model_dump(mode="json")})
            if _found(state.comparison):
                expansions_since_found = 0 if expansions_since_found is None else expansions_since_found + 1
                if expansions_since_found >= search.frontier.expansions_after_first_path:
                    stop = "path_found"
            if cap_hit:
                stop = cap_hit

        return _finish(state, stop or "unknown", policy)
    except Exception as exc:  # never leave a run hanging without a terminal event
        log.exception("investigation %s crashed", state.investigation_id)
        return fail("internal_error", f"{type(exc).__name__}: {exc}")


def _finish(state: InvestigationState, stop: str, policy: RelationshipPolicy) -> InvestigationState:
    found = _found(state.comparison)
    state.stop_reason = stop
    payload: dict[str, Any] = {
        "outcome": "path_found" if found else "no_path_found",
        "stop_reason": stop,
        "scope": "explored_graph",
        "comparison": state.comparison.model_dump(mode="json") if state.comparison else None,
        "stats": {
            "expanded_nodes": len(state.expansions),
            "truncated_expansions": sum(1 for r in state.expansions if not r.complete and r.error is None),
            "failed_expansions": sum(1 for r in state.expansions if r.error),
            "explored_nodes": len(state.graph.nodes),
            "explored_edges": len(state.graph.edges),
            "provider_requests": state.provider_requests,
        },
        "note": ("Paths are shortest/strongest among the explored relationships only; "
                 "Wikidata statements are what Wikidata records, not verified facts."),
    }
    if stop in ("path_found", "frontier_exhausted"):
        state.status = "completed"
        state.events.emit("investigation.completed", payload)
    else:
        state.status = "partial"
        state.events.emit("investigation.partial", payload)
    return state
