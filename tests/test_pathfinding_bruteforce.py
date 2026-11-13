"""Cross-check shortest/strongest against brute-force enumeration of all simple paths."""

import math
import random

import pytest

from src.graph.model import Edge
from src.graph.paths import compare_paths

from .graph_helpers import SRC, graph, org, person, policy

PIDS = ["P26", "P22", "P25", "P3373", "P40", "P451", "P1038", "P1066", "P108", "P69", "P463", "P54", "P39"]


def brute_force(g, p, source, target, pids):
    """All simple eligible paths source->target with <= max_hops edges -> list of (hops, cost)."""
    H, cap = p.pathfinding.max_hops, p.pathfinding.max_intermediate_degree
    by_kind = p.pathfinding.max_intermediate_degree_by_kind

    def blocked(q):
        if q in (source, target):
            return False
        n = g.node(q)
        c = by_kind.get(n.kind, cap)  # per-kind override, else the global cap
        return c is not None and n.kind != "person" and n.degree > c

    def step(edge, into):
        c = -math.log(max(p.ranking.minimum_strength, p.properties[edge.pid].strength)) + p.ranking.hop_penalty
        n = g.node(into)
        if into not in (source, target) and n.kind != "person":
            c += p.ranking.hub_penalty_weight * math.log1p(n.degree)
        return c

    found = []

    def dfs(v, visited, hops, cost):
        if v == target:
            found.append((hops, cost))
            return
        if hops == H or (v != source and blocked(v)):
            return
        for e in g.incident(v, pids):
            w = e.other(v)
            if w in visited or blocked(w):
                continue
            dfs(w, visited | {w}, hops + 1, cost + step(e, w))

    dfs(source, {source}, 0, 0.0)
    return found


def random_case(rng: random.Random):
    n_people, n_orgs = rng.randint(4, 9), rng.randint(0, 4)
    people = [f"Q95{i:04d}" for i in range(n_people)]
    orgs = [f"Q96{i:04d}" for i in range(n_orgs)]
    triples = set()
    for _ in range(rng.randint(3, 18)):
        s = rng.choice(people)
        pid = rng.choice(PIDS)
        if pid in ("P108", "P69", "P463", "P39") and orgs:
            o = rng.choice(orgs)
        else:
            o = rng.choice(people)
        if s != o:
            triples.add((s, pid, o))
    g = graph([person(q) for q in people] + [org(q) for q in orgs], sorted(triples), complete=True)
    p = policy(max_hops=rng.randint(1, 5), max_intermediate_degree=rng.choice([None, 2, 3, 5]),
               max_intermediate_degree_by_kind=rng.choice([{}, {"organization": 2}, {"organization": None}]))
    groups = set(p.default_groups) | ({"position"} if rng.random() < 0.3 else set())
    source, target = rng.sample(people, 2)
    return g, p, groups, source, target


def dense_case(rng: random.Random):
    """Denser graphs biased toward org shortcuts plus interpersonal chains."""
    people = [f"Q95{i:04d}" for i in range(rng.randint(6, 12))]
    orgs = [f"Q96{i:04d}" for i in range(rng.randint(1, 3))]
    triples = set()
    for _ in range(rng.randint(10, 26)):
        s = rng.choice(people)
        r = rng.random()
        if r < 0.3:
            pid, o = rng.choice(["P108", "P69", "P463", "P54"]), rng.choice(orgs)
        elif r < 0.4:  # work -> person credit (reversed shape)
            s, pid, o = rng.choice(orgs), rng.choice(["P161", "P57", "P175"]), s
        else:
            pid, o = rng.choice(["P26", "P22", "P25", "P3373", "P40", "P451", "P1038", "P1066"]), rng.choice(people)
        if s != o:
            triples.add((s, pid, o))
    g = graph([person(q) for q in people] + [org(q) for q in orgs], sorted(triples), complete=True)
    p = policy(max_hops=rng.randint(3, 6), max_intermediate_degree=rng.choice([None, 6, 10]))
    source, target = rng.sample(people, 2)
    return g, p, set(p.default_groups), source, target


def _divergent_seeds(n: int) -> list[int]:
    """Seeds where BRUTE FORCE (not the engine) shows the cheapest path is longer than the shortest."""
    seeds, seed = [], 0
    while len(seeds) < n:
        g, p, groups, s, t = dense_case(random.Random(seed))
        paths = brute_force(g, p, s, t, p.active_pids(groups))
        if paths:
            min_hops = min(h for h, _ in paths)
            cheapest_hops = min(paths, key=lambda hc: (hc[1], hc[0]))[0]
            if cheapest_hops > min_hops:
                seeds.append(seed)
        seed += 1
    return seeds


CASES = [("random", s) for s in range(400)] + [("divergent", s) for s in _divergent_seeds(150)]


@pytest.mark.parametrize("kind,seed", CASES)
def test_matches_brute_force(kind, seed):
    make = random_case if kind == "random" else dense_case
    g, p, groups, source, target = make(random.Random(seed))
    paths = brute_force(g, p, source, target, p.active_pids(groups))
    cmp = compare_paths(g, source, target, p, groups)

    if not paths:
        assert cmp.shortest.status == cmp.strongest.status == "no_path_within_bounds"
        return

    min_hops = min(h for h, _ in paths)
    min_cost_at_min_hops = min(c for h, c in paths if h == min_hops)
    min_cost = min(c for _, c in paths)

    short, strong = cmp.shortest.result, cmp.strongest.result
    assert cmp.shortest.status == cmp.strongest.status == "found"
    assert short.hop_count == min_hops
    assert short.total_cost == pytest.approx(min_cost_at_min_hops, abs=1e-6)
    assert strong.total_cost == pytest.approx(min_cost, abs=1e-6)
    assert strong.hop_count <= p.pathfinding.max_hops
    if kind == "divergent":
        assert not cmp.same_path and strong.hop_count > short.hop_count
    for res in (short, strong):
        qids = [n.qid for n in res.nodes]
        assert len(qids) == len(set(qids)), "path must be simple"
        assert qids[0] == source and qids[-1] == target
        assert all(Edge(h.subject, h.pid, h.object, SRC).edge_id == h.edge_id for h in res.hops)
