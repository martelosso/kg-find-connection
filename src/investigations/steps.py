"""Building blocks shared by the deterministic baseline and the agent investigator.

Both strategies start, expand, recompute paths and finish through these functions, so their
event streams have the same shape and the same honesty rules (plan §6/§7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import RelationshipPolicy, SearchConfig
from ..graph.model import Node
from ..graph.paths import compare_paths
from ..models import PathComparison
from ..wikidata.provider import Direction, ExpansionBatch, KnowledgeProvider, ProviderError
from .events import edge_payload, node_payload
from .state import ExpansionRecord, InvestigationState

NOTE = ("Paths are shortest/strongest among the explored relationships only; "
        "Wikidata statements are what Wikidata records, not verified facts.")


def strength_priority(policy: RelationshipPolicy, pids) -> list[str]:
    return sorted(pids, key=lambda p: (-policy.properties[p].strength, int(p[1:])))


def edge_labels(policy: RelationshipPolicy, pid: str) -> dict[str, Any]:
    prop = policy.properties.get(pid)
    if prop is None:
        return {"label": pid, "label_es": pid, "group": None}
    return {"label": prop.label, "label_es": prop.label_es, "group": prop.group}


def budget_stop(state: InvestigationState) -> str | None:
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


def found(cmp: PathComparison | None) -> bool:
    return cmp is not None and cmp.shortest.result is not None


def path_ids(cmp: PathComparison | None) -> tuple[str | None, str | None]:
    if cmp is None:
        return (None, None)
    return (cmp.shortest.result.path_id if cmp.shortest.result else None,
            cmp.strongest.result.path_id if cmp.strongest.result else None)


def fail(state: InvestigationState, code: str, message: str) -> InvestigationState:
    state.status, state.stop_reason = "failed", code
    state.events.emit("investigation.failed", {"error": {"code": code, "message": message}})
    return state


def begin(state: InvestigationState, provider: KnowledgeProvider, policy: RelationshipPolicy,
          search: SearchConfig, extra: dict[str, Any] | None = None) -> bool:
    """Emit investigation.started and resolve both endpoints. False (after a failed event) on error."""
    s, t = state.source_qid, state.target_qid
    pids = policy.active_pids(state.groups)
    state.status = "running"
    state.events.emit("investigation.started", {
        "source_qid": s, "target_qid": t, "strategy": state.strategy, "run_mode": state.run_mode,
        "provider": provider.name, "snapshot": provider.snapshot, "groups": sorted(state.groups),
        "properties": sorted(pids, key=lambda p: int(p[1:])), "policy_version": policy.version,
        "search_config_version": search.version, "budgets": state.budgets.model_dump(),
        "max_hops": policy.pathfinding.max_hops,
        "max_intermediate_degree": policy.pathfinding.max_intermediate_degree,
        **(extra or {}),
    })
    if s == t:
        fail(state, "invalid_request", "source and target must differ")
        return False
    try:
        records = {r.qid: r for r in provider.get_entities([s, t])}
    except ProviderError as exc:
        fail(state, exc.code, exc.message)
        return False
    for q in (s, t):
        if q not in records:
            fail(state, "unknown_entity", f"{q} is not in the {provider.name} store")
            return False
        if records[q].kind != "person":
            fail(state, "not_a_person", f"{q} is not recorded as a human (P31 Q5)")
            return False
    endpoints = [records[s].to_node(), records[t].to_node()]
    for n in endpoints:
        state.graph.add_node(n)
    state.events.emit("identity.resolved", {
        "source": node_payload(endpoints[0]), "target": node_payload(endpoints[1]),
        "added_nodes": [node_payload(n) for n in endpoints], "added_edges": [],
    })
    return True


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


@dataclass
class ExpandOutcome:
    ok: bool
    error: dict[str, str] | None = None
    added_nodes: list[dict] = field(default_factory=list)
    added_edges: list[dict] = field(default_factory=list)
    returned: int = 0
    complete: bool = False
    cap_hit: str | None = None
    paths_changed: bool = False


def expand_step(
    state: InvestigationState,
    provider: KnowledgeProvider,
    policy: RelationshipPolicy,
    *,
    tool: str,
    step: int,
    qid: str,
    side: int,
    pids: frozenset[str],
    direction: Direction,
    max_edges: int,
    started_extra: dict[str, Any] | None = None,
) -> ExpandOutcome:
    """One provider expansion with its tool.started / graph.expanded / tool.completed events."""
    ev = state.events
    priority = strength_priority(policy, pids)
    node = state.graph.node(qid)
    side_name = "source" if side == 0 else "target"
    ev.emit("tool.started", {
        "tool": tool, "step": step, "qid": qid, "label": node.label() if node else qid, "side": side_name,
        **(started_extra or {}),
        "arguments": {"qids": [qid], "properties": priority, "direction": direction, "max_edges": max_edges},
    })
    state.provider_requests += 1
    try:
        batch = provider.expand([qid], pids, direction, max_edges, pid_priority=priority)
    except ProviderError as exc:
        state.expansions.append(ExpansionRecord(qid, side, tuple(priority), direction, 0, False, 0, 0,
                                                error=exc.code))
        error = {"code": exc.code, "message": exc.message}
        ev.emit("tool.completed", {"tool": tool, "step": step, "qid": qid, "ok": False, "error": error})
        return ExpandOutcome(ok=False, error=error)

    added_nodes, added_edges, cap_hit = apply_batch(state, batch, policy)
    cov = batch.coverage[0]
    complete = cov.complete and cap_hit is None
    state.expansions.append(ExpansionRecord(qid, side, tuple(priority), direction, cov.returned, complete,
                                            len(added_nodes), len(added_edges)))
    ev.emit("graph.expanded", {
        "step": step, "expanded_qid": qid, "side": side_name,
        "added_nodes": added_nodes, "added_edges": added_edges,
        "highlight_edge_ids": [e["id"] for e in added_edges],
        "returned": cov.returned, "complete": complete, "truncated": not complete,
        "cap_hit": cap_hit, "provider": batch.provider, "snapshot": batch.snapshot,
        "source_urls": batch.source_urls,
    })
    ev.emit("tool.completed", {
        "tool": tool, "step": step, "qid": qid, "ok": True, "returned": cov.returned,
        "added_node_count": len(added_nodes), "added_edge_count": len(added_edges), "complete": complete,
    })
    changed = update_paths(state, policy, step, qid) if added_edges else False
    return ExpandOutcome(ok=True, added_nodes=added_nodes, added_edges=added_edges, returned=cov.returned,
                         complete=complete, cap_hit=cap_hit, paths_changed=changed)


def update_paths(state: InvestigationState, policy: RelationshipPolicy, step: int, trigger_qid: str) -> bool:
    before = path_ids(state.comparison)
    cmp = compare_paths(state.graph, state.source_qid, state.target_qid, policy, state.groups)
    state.comparison = cmp
    if path_ids(cmp) == before:
        return False
    state.events.emit("paths.updated", {"step": step, "trigger_qid": trigger_qid,
                                        "comparison": cmp.model_dump(mode="json")})
    return True


def finish(state: InvestigationState, stop: str, *, completed: bool,
           extra: dict[str, Any] | None = None) -> InvestigationState:
    state.stop_reason = stop
    payload: dict[str, Any] = {
        "outcome": "path_found" if found(state.comparison) else "no_path_found",
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
        "note": NOTE,
        **(extra or {}),
    }
    state.status = "completed" if completed else "partial"
    state.events.emit("investigation.completed" if completed else "investigation.partial", payload)
    return state
