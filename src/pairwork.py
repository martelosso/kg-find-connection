"""Evaluate independent (source, target) pairs in parallel worker processes (prep scripts only).

The prep scripts (find_demo_pairs, analyze_hubs, benchmark_investigations) run an exact path
search, and sometimes a budgeted investigation, for each of hundreds of pairs. The pairs are
independent, so ``map_pairs`` spreads them over processes:

* each worker opens the people graph ONCE (``IndexedPeopleGraph``: memory-mapped arrays shared
  with the other workers through the OS page cache) and, if a task needs it, a SQLite provider;
* results come back in INPUT order, so a script's output does not depend on ``workers``;
* ``workers <= 1`` runs in-process with the same code path (tests, small runs);
* stopping early (breaking out of the iteration) cancels the pairs not yet started.

Tasks are plain module-level functions ``task(ctx, source, target, **kwargs)`` so they can be sent
to worker processes; ``ctx`` holds ``graph``, ``policy``, ``search`` and ``provider()``.
Nothing here is used by the server or at demo time.
"""

from __future__ import annotations

import os
from itertools import repeat
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .config import RelationshipPolicy, load_policy, load_search_config

_CTX: dict[str, Any] = {}


class Context:
    """Per-process state for pair tasks: the graph, the policies and a lazily opened provider."""

    def __init__(self, db: str | Path, graph=None) -> None:
        from .graph.store import IndexedPeopleGraph

        self.db = str(db)
        self.graph = graph if graph is not None else IndexedPeopleGraph(self.db)
        self.policy = load_policy()
        self.search = load_search_config()
        self._provider = None

    def provider(self):
        if self._provider is None:
            from .wikidata.offline import SqlitePeopleGraphProvider

            self._provider = SqlitePeopleGraphProvider(self.db)
        return self._provider


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def _init(db: str) -> None:
    _CTX["ctx"] = Context(db)


def _call(task: Callable, source: str, target: str, kwargs: dict) -> Any:
    return task(_CTX["ctx"], source, target, **kwargs)


def map_pairs(db: str | Path, task: Callable, pairs: Iterable[tuple[str, str]], workers: int,
              context: Context | None = None, **kwargs) -> Iterator[Any]:
    """Yield ``task(ctx, s, t, **kwargs)`` for each pair, in input order."""
    pairs = list(pairs)
    if workers <= 1 or len(pairs) <= 1:
        ctx = context or Context(db)
        for s, t in pairs:
            yield task(ctx, s, t, **kwargs)
        return
    from concurrent.futures import ProcessPoolExecutor

    pool = ProcessPoolExecutor(min(workers, len(pairs)), initializer=_init, initargs=(str(db),))
    try:
        yield from pool.map(_call, repeat(task), [s for s, _ in pairs], [t for _, t in pairs], repeat(kwargs))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def with_cap(policy: RelationshipPolicy, cap: int | None) -> RelationshipPolicy:
    return policy.model_copy(update={"pathfinding": policy.pathfinding.model_copy(
        update={"max_intermediate_degree": cap})})


# ---- tasks ----------------------------------------------------------------------------------------

INTERPERSONAL = {"family", "mentorship"}


def demo_pair_row(ctx: Context, source: str, target: str, *, max_hops: int) -> dict | None:
    """find_demo_pairs: exact paths, then a budgeted live run. None if not a 2..max_hops connection."""
    import time

    from .graph.paths import compare_paths
    from .investigations.baseline import run_deterministic
    from .investigations.state import InvestigationState

    t0 = time.time()
    exact = compare_paths(ctx.graph, source, target, ctx.policy)
    exact_s = time.time() - t0
    short, strong = exact.shortest.result, exact.strongest.result
    if short is None or short.hop_count < 2 or short.hop_count > max_hops:
        return None
    strongest_status = exact.strongest.status
    if strong is None:  # the weighted search gave up (work caps); report the shortest path instead
        strong = short
    nodes = {n.qid: n for r in (short, strong) for n in r.nodes}
    provider = ctx.provider()
    state = InvestigationState(source, target, ctx.policy.default_groups, ctx.search.budgets,
                               snapshot=provider.snapshot)
    t0 = time.time()
    run_deterministic(state, provider, ctx.policy, ctx.search)
    live = state.comparison.shortest.result if state.comparison else None
    return {
        "source": source, "target": target,
        "source_label": ctx.graph.node(source).label(), "target_label": ctx.graph.node(target).label(),
        "shortest_hops": short.hop_count, "strongest_hops": strong.hop_count,
        "shortest_cost": round(short.total_cost, 3), "strongest_cost": round(strong.total_cost, 3),
        "divergent": not exact.same_path, "strongest_status": strongest_status,
        "interpersonal_hops": sum(h.relation_group in INTERPERSONAL for h in strong.hops),
        "unlabeled_nodes": sum(1 for n in nodes.values() if not n.label_en),
        "strongest_route": " → ".join(n.label_en or n.qid for n in strong.nodes),
        "live_found": live is not None, "live_hops": live.hop_count if live else None,
        "live_requests": state.provider_requests, "live_stop": state.stop_reason,
        "exact_seconds": round(exact_s, 2), "live_seconds": round(time.time() - t0, 2),
    }


def hub_sweep_pair(ctx: Context, source: str, target: str, *, caps: list[int | None],
                   groups: frozenset[str] | None, hub_floor: int) -> list[dict]:
    """analyze_hubs: exact shortest/strongest under each candidate cap, one record per cap."""
    import time

    from .graph.paths import compare_paths

    out = []
    for cap in caps:
        t0 = time.perf_counter()
        cmp = compare_paths(ctx.graph, source, target, with_cap(ctx.policy, cap), groups)
        rec: dict[str, Any] = {"seconds": time.perf_counter() - t0,
                               "truncated": cmp.shortest.status == "budget_exhausted"
                               or cmp.strongest.status == "budget_exhausted",
                               "connected": False}
        short, strong = cmp.shortest.result, cmp.strongest.result
        if short is not None:
            orgs = [n.qid for n in short.nodes[1:-1] if n.kind != "person"]
            degs = [ctx.graph.node_profile(q)[1] for q in orgs]
            rec.update(connected=True, short_hops=short.hop_count, short_via_org=bool(orgs),
                       max_org_degree=max(degs) if degs else None,
                       hubs=[q for q, d in zip(orgs, degs) if d >= hub_floor])
            if strong is not None:
                rec.update(strong_hops=strong.hop_count, divergent=not cmp.same_path,
                           strong_interpersonal_only=all(h.relation_group in INTERPERSONAL for h in strong.hops),
                           strong_has_interpersonal=any(h.relation_group in INTERPERSONAL for h in strong.hops))
        out.append(rec)
    return out


def benchmark_pair(ctx: Context, source: str, target: str) -> dict | None:
    """benchmark_investigations: exact shortest hops vs the budgeted live run. None if unconnected."""
    import time

    from .graph.paths import compute_shortest_path
    from .investigations.baseline import run_deterministic
    from .investigations.state import InvestigationState

    t0 = time.time()
    exact = compute_shortest_path(ctx.graph, source, target, ctx.policy)
    exact_s = time.time() - t0
    if exact.result is None:
        return None
    provider = ctx.provider()
    state = InvestigationState(source, target, ctx.policy.default_groups, ctx.search.budgets,
                               snapshot=provider.snapshot)
    t0 = time.time()
    run_deterministic(state, provider, ctx.policy, ctx.search)
    found = state.comparison.shortest.result if state.comparison else None
    return {"source": source, "target": target, "full_hops": exact.result.hop_count,
            "found_hops": found.hop_count if found else None, "stop": state.stop_reason,
            "expansions": len(state.expansions), "exact_s": exact_s, "inv_s": time.time() - t0}
