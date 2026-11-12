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

from ..config import RelationshipPolicy, SearchConfig
from ..graph.frontier import ExpansionCandidate, rank_frontier
from ..wikidata.provider import KnowledgeProvider
from .state import InvestigationState
from .steps import apply_batch, begin, budget_stop, edge_labels, expand_step, fail, finish, found

__all__ = ["run_deterministic", "apply_batch", "edge_labels"]

log = logging.getLogger(__name__)

TOOL = "expand_neighbors"


def _choose(cands: list[ExpansionCandidate], state: InvestigationState) -> ExpansionCandidate:
    """Balance the two searches: take the best candidate of the side with fewer expansions."""
    done = [0, 0]
    for r in state.expansions:
        done[r.side] += 1
    for side in sorted((0, 1), key=lambda s: (done[s], s)):
        for c in cands:
            if c.side == side:
                return c
    return cands[0]


def run_deterministic(
    state: InvestigationState,
    provider: KnowledgeProvider,
    policy: RelationshipPolicy,
    search: SearchConfig,
) -> InvestigationState:
    s, t = state.source_qid, state.target_qid
    pids = policy.active_pids(state.groups)
    try:
        if not begin(state, provider, policy, search):
            return state
        since_found: int | None = None
        stop: str | None = None
        step = 0
        while stop is None:
            stop = budget_stop(state)
            if stop:
                break
            cands = rank_frontier(state.graph, s, t, state.expanded, policy, search.frontier, state.groups)
            if not cands:
                stop = "frontier_exhausted"
                break
            cand = _choose(cands, state)
            step += 1
            out = expand_step(
                state, provider, policy, tool=TOOL, step=step, qid=cand.qid, side=cand.side, pids=pids,
                direction="both", max_edges=state.budgets.max_edges_per_expansion,
                started_extra={"reason": cand.reason, "score": round(cand.score, 6),
                               "via_edge_id": cand.via_edge_id},
            )
            if found(state.comparison):
                since_found = 0 if since_found is None else since_found + 1
                if since_found >= search.frontier.expansions_after_first_path:
                    stop = "path_found"
            if out.cap_hit:
                stop = out.cap_hit
        return finish(state, stop, completed=stop in ("path_found", "frontier_exhausted"))
    except Exception as exc:  # never leave a run hanging without a terminal event
        log.exception("investigation %s crashed", state.investigation_id)
        return fail(state, "internal_error", f"{type(exc).__name__}: {exc}")
