"""Typed agent tools (plan §6). Inputs are Pydantic-validated; outputs are compact JSON for the model.

Allowlists enforced here, not in the prompt:
- ``get_neighbors`` only for QIDs already in the explored graph, never for hubs that cannot be
  intermediate nodes, only for ENABLED relation groups, ``limit`` <= max_edges_per_expansion;
- every call counts against ``max_agent_tool_calls``; expansions also count against provider budgets.

Errors come back as ``{"ok": false, "error": {"code", "message"}}`` and never add graph data.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..config import RelationshipPolicy, SearchConfig
from ..graph.frontier import rank_frontier
from ..graph.model import is_qid
from ..models import PathResult
from ..wikidata.provider import KnowledgeProvider, ProviderError
from ..investigations.state import InvestigationState
from ..investigations.steps import budget_stop, expand_step

MAX_LABEL = 80


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolvePersonArgs(_Args):
    name: str = Field(min_length=2, max_length=120, description="Name to look up (prefix match on labels).")
    limit: int = Field(default=5, ge=1, le=8)


class GetNeighborsArgs(_Args):
    qid: str = Field(description="QID of a node already in the explored graph, e.g. Q7186.")
    relation_groups: list[str] = Field(min_length=1, description="Enabled relation groups to request.")
    direction: Literal["outgoing", "incoming", "both"] = "both"
    limit: int | None = Field(default=None, ge=1, description="Maximum edges to fetch (capped by budget).")


class NoArgs(_Args):
    pass


class InspectPathArgs(_Args):
    path_id: str = Field(min_length=1, max_length=64)


class FinishArgs(_Args):
    summary: str = Field(min_length=1, max_length=2000,
                         description="1-3 sentences for the audience; mention only people on the backend paths.")


TOOL_SPECS: dict[str, tuple[type[_Args], str]] = {
    "resolve_person": (ResolvePersonArgs, "Look up people by name in the snapshot (read-only, no graph change)."),
    "get_neighbors": (GetNeighborsArgs, "Expand one explored node: fetch its relationships in the given groups."),
    "get_search_status": (NoArgs, "Explored size, remaining budget, current paths, ranked expansion suggestions."),
    "compute_paths": (NoArgs, "Current shortest and strongest paths on the explored graph (backend computed)."),
    "inspect_path": (InspectPathArgs, "Edge-by-edge evidence for a path returned by compute_paths."),
    "finish": (FinishArgs, "Stop exploring and give the final plain-language summary."),
}


def tool_definitions() -> list[dict[str, Any]]:
    out = []
    for name, (model, description) in TOOL_SPECS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        out.append({"type": "function", "function": {"name": name, "description": description, "parameters": schema}})
    return out


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _label(text: str | None, qid: str) -> str:
    text = " ".join((text or qid).split())
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def path_summary(r: PathResult | None) -> dict[str, Any] | None:
    if r is None:
        return None
    labels = {n.qid: _label(n.label_en or n.label_es, n.qid) for n in r.nodes}
    return {
        "path_id": r.path_id,
        "hops": r.hop_count,
        "cost": round(r.total_cost, 3),
        "route": [labels[r.source_qid]] + [f"-[{h.label}]-> {labels[h.to_qid]}" for h in r.hops],
        "qids": [n.qid for n in r.nodes],
    }


class Toolbox:
    """Executes tool calls against one investigation's state."""

    def __init__(self, state: InvestigationState, provider: KnowledgeProvider, policy: RelationshipPolicy,
                 search: SearchConfig) -> None:
        self.state, self.provider, self.policy, self.search = state, provider, policy, search
        self.finished_summary: str | None = None
        self.consecutive_no_progress = 0
        self._handlers: dict[str, Callable[[Any, int, str], dict[str, Any]]] = {
            "resolve_person": self._resolve_person,
            "get_neighbors": self._get_neighbors,
            "get_search_status": lambda a, step, why: self.status(),
            "compute_paths": lambda a, step, why: self._paths(),
            "inspect_path": self._inspect_path,
            "finish": self._finish,
        }

    # ---- dispatch -----------------------------------------------------------------------------

    def call(self, name: str, raw_arguments: str, step: int, decision: str) -> dict[str, Any]:
        """Validate and run one tool call; emits events. Always returns a JSON-able result."""
        ev = self.state.events
        self.state.tool_calls += 1
        try:
            if name not in TOOL_SPECS:
                raise ToolError("unknown_tool", f"no tool named {name!r}; available: {sorted(TOOL_SPECS)}")
            try:
                data = json.loads(raw_arguments or "{}")
                if not isinstance(data, dict):
                    raise ValueError("arguments must be a JSON object")
                args = TOOL_SPECS[name][0].model_validate(data)
            except (ValueError, ValidationError) as exc:
                raise ToolError("invalid_arguments", str(exc)[:500]) from exc
            if name == "get_neighbors":
                return self._handlers[name](args, step, decision)  # emits its own tool events
            ev.emit("tool.started", {"tool": name, "step": step, "arguments": args.model_dump()})
            result = {"ok": True, **self._handlers[name](args, step, decision)}
            ev.emit("tool.completed", {"tool": name, "step": step, "ok": True})
            return result
        except ToolError as exc:
            error = {"code": exc.code, "message": exc.message}
            ev.emit("tool.started", {"tool": name, "step": step, "arguments": {"raw": raw_arguments[:500]}})
            ev.emit("tool.completed", {"tool": name, "step": step, "ok": False, "error": error})
            if name == "get_neighbors":
                self.consecutive_no_progress += 1
            return {"ok": False, "error": error}

    # ---- tools --------------------------------------------------------------------------------

    def _resolve_person(self, args: ResolvePersonArgs, step: int, why: str) -> dict[str, Any]:
        try:
            matches = self.provider.search_people(args.name, args.limit)
        except ProviderError as exc:
            raise ToolError(exc.code, exc.message) from exc
        return {"matches": [{"qid": m.qid, "label": _label(m.label_en or m.label_es, m.qid), "degree": m.degree}
                            for m in matches]}

    def _side(self, qid: str) -> int:
        """0 if the node is (weakly) closer to the source in the explored graph, else 1."""
        pids = self.policy.active_pids(self.state.groups)
        dist = []
        for start in (self.state.source_qid, self.state.target_qid):
            seen, queue = {start: 0}, deque([start])
            while queue and qid not in seen:
                v = queue.popleft()
                for e in self.state.graph.incident(v, pids):
                    w = e.other(v)
                    if w not in seen:
                        seen[w] = seen[v] + 1
                        queue.append(w)
            dist.append(seen.get(qid, 10**6))
        return 0 if dist[0] <= dist[1] else 1

    def _get_neighbors(self, args: GetNeighborsArgs, step: int, why: str) -> dict[str, Any]:
        st, pol = self.state, self.policy
        if not is_qid(args.qid):
            raise ToolError("invalid_arguments", f"{args.qid!r} is not a QID")
        node = st.graph.node(args.qid)
        if node is None:
            raise ToolError("not_allowed", f"{args.qid} is not in the explored graph; expand a node you have seen")
        unknown = set(args.relation_groups) - set(pol.groups)
        if unknown:
            raise ToolError("invalid_arguments", f"unknown relation groups {sorted(unknown)}")
        disabled = set(args.relation_groups) - st.groups
        if disabled:
            raise ToolError("not_allowed", f"relation groups {sorted(disabled)} are not enabled for this investigation")
        cap = pol.pathfinding.max_intermediate_degree
        if (cap is not None and node.kind != "person" and node.degree > cap
                and args.qid not in (st.source_qid, st.target_qid)):
            raise ToolError("not_allowed", f"{args.qid} has degree {node.degree} > {cap}: hubs cannot be intermediate nodes")
        pids = pol.active_pids(frozenset(args.relation_groups))
        for r in st.expansions:
            if r.qid == args.qid and r.error is None and r.complete and set(pids) <= set(r.properties) \
                    and r.direction in ("both", args.direction):
                raise ToolError("already_expanded", f"{args.qid} was already fully expanded for these groups")
        stop = budget_stop(st)
        if stop:
            raise ToolError("budget_exhausted", f"budget exhausted: {stop}")
        max_edges = min(args.limit or st.budgets.max_edges_per_expansion, st.budgets.max_edges_per_expansion)

        out = expand_step(st, self.provider, pol, tool="get_neighbors", step=step, qid=args.qid,
                          side=self._side(args.qid), pids=pids, direction=args.direction, max_edges=max_edges,
                          started_extra={"reason": why, "relation_groups": sorted(args.relation_groups)})
        if not out.ok:
            self.consecutive_no_progress += 1
            return {"ok": False, "error": out.error}
        self.consecutive_no_progress = 0 if out.added_edges else self.consecutive_no_progress + 1
        limit = self.search.agent.max_result_nodes
        return {
            "ok": True,
            "expanded": args.qid,
            "added_edges": len(out.added_edges),
            "complete": out.complete,
            "cap_hit": out.cap_hit,
            "added_nodes": [{"qid": n["id"], "label": _label(n["label"], n["id"]), "kind": n["kind"],
                             "degree": n["degree"]} for n in out.added_nodes[:limit]],
            "more_added_nodes": max(0, len(out.added_nodes) - limit),
            "paths_changed": out.paths_changed,
            "remaining": self.remaining(),
            "path_found": bool(st.comparison and st.comparison.shortest.result),
            **({"strongest_cost": round(st.comparison.strongest.result.total_cost, 3), **self.improvement_leads()}
               if st.comparison and st.comparison.strongest.result else {}),
        }

    def remaining(self) -> dict[str, int]:
        st, b = self.state, self.state.budgets
        return {
            "tool_calls": max(0, b.max_agent_tool_calls - st.tool_calls),
            "neighbor_requests": max(0, b.max_provider_requests - st.provider_requests),
            "nodes": max(0, b.max_explored_nodes - len(st.graph.nodes)),
            "edges": max(0, b.max_explored_edges - len(st.graph.edges)),
        }

    def status(self) -> dict[str, Any]:
        st, b = self.state, self.state.budgets
        cands = rank_frontier(st.graph, st.source_qid, st.target_qid, st.expanded, self.policy,
                              self.search.frontier, st.groups)[: self.search.agent.status_frontier_size]
        return {
            "explored_nodes": len(st.graph.nodes),
            "explored_edges": len(st.graph.edges),
            "expanded": [{"qid": r.qid, "complete": r.complete, "error": r.error} for r in st.expansions],
            "remaining": self.remaining(),
            "enabled_groups": sorted(st.groups),
            **self._paths(),
            "suggestions": [{"qid": c.qid, "label": _label(st.graph.node(c.qid).label(), c.qid),  # type: ignore[union-attr]
                             "side": "source" if c.side == 0 else "target", "hops": c.hops,
                             "degree": c.degree, "score": round(c.score, 3)} for c in cands],
        }

    def _paths(self) -> dict[str, Any]:
        cmp = self.state.comparison
        if cmp is None or cmp.shortest.result is None:
            return {"shortest": None, "strongest": None, "same_path": False, "scope": "explored_graph"}
        return {"shortest": path_summary(cmp.shortest.result), "strongest": path_summary(cmp.strongest.result),
                "same_path": cmp.same_path, "scope": cmp.scope, **self.improvement_leads()}

    def improvement_leads(self) -> dict[str, Any]:
        """Unexpanded nodes through which a STRONGER path could still exist (backend-computed).

        Any path not yet in the explored graph leaves it at an unexpanded node ``a`` reached from the
        source and re-enters at a different unexpanded node ``b`` reached from the target, using at
        least one unseen edge. Its cost is therefore at least
        ``source_cost(a) + min_edge + target_cost(b)``. ``a`` is a lead if that bound, with the best
        partner ``b``, beats the current strongest cost (and symmetrically for ``b``). If there are no
        leads, no further expansion of a not-yet-expanded node can beat the current strongest path.
        (Nodes whose earlier expansion was TRUNCATED may still hide edges; they are not leads, so
        this is guidance for the agent, never a claim of optimality.)
        """
        st, pol = self.state, self.policy
        strongest = st.comparison.strongest.result if st.comparison else None
        if strongest is None:
            return {}
        pids = pol.active_pids(st.groups)
        min_edge = min(pol.strength_cost(p) for p in pids) + pol.ranking.hop_penalty
        cands = rank_frontier(st.graph, st.source_qid, st.target_qid, st.expanded, pol, self.search.frontier, st.groups)

        def two_smallest(side_attr: str) -> list[tuple[float, str]]:
            vals = sorted((getattr(c, side_attr), c.qid) for c in cands if getattr(c, side_attr) is not None)
            return vals[:2]

        best_src, best_tgt = two_smallest("source_cost"), two_smallest("target_cost")

        def partner(best: list[tuple[float, str]], qid: str) -> float | None:
            for cost, q in best:
                if q != qid:
                    return cost
            return None

        leads = []
        for c in cands:
            bounds = []
            if c.source_cost is not None and (p := partner(best_tgt, c.qid)) is not None:
                bounds.append(c.source_cost + min_edge + p)
            if c.target_cost is not None and (p := partner(best_src, c.qid)) is not None:
                bounds.append(c.target_cost + min_edge + p)
            if bounds and min(bounds) < strongest.total_cost - 1e-9:
                leads.append((min(bounds), c))
        leads.sort(key=lambda bc: (bc[0], bc[1].hops, int(bc[1].qid[1:])))
        limit = self.search.agent.status_frontier_size
        return {
            "stronger_path_possible": bool(leads),
            "improvement_leads": [
                {"qid": c.qid, "label": _label(st.graph.node(c.qid).label(), c.qid),  # type: ignore[union-attr]
                 "side": "source" if c.side == 0 else "target", "best_possible_cost": round(bound, 3),
                 "degree": c.degree} for bound, c in leads[:limit]],
            "more_leads": max(0, len(leads) - limit),
        }

    def _inspect_path(self, args: InspectPathArgs, step: int, why: str) -> dict[str, Any]:
        cmp = self.state.comparison
        for r in (cmp.shortest.result, cmp.strongest.result) if cmp else ():
            if r is not None and r.path_id == args.path_id:
                return {"path_id": r.path_id, "mode": r.mode, "scope": r.scope, "snapshot": r.snapshot, "edges": [
                    {"edge_id": h.edge_id, "subject": h.subject, "property": h.pid, "object": h.object,
                     "label": h.label, "direction": h.direction, "group": h.relation_group,
                     "cost": round(h.cost.total, 3), "source": h.source, "property_url": h.property_url}
                    for h in r.hops]}
        raise ToolError("unknown_path", f"{args.path_id!r} is not a current path; call compute_paths")

    def _finish(self, args: FinishArgs, step: int, why: str) -> dict[str, Any]:
        self.finished_summary = args.summary
        return {"finished": True}
