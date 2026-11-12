"""Deterministic path engine tests on synthetic graphs (no LLM, no network)."""

import math

import pytest

from src.graph.paths import compare_paths, compute_shortest_path, compute_strongest_path
from src.graph.model import Edge, Node

from .graph_helpers import A, B, C, D, E, R, SRC, U, X, Y, graph, org, person, policy


def chain(qids: list[str], pid: str = "P3373") -> list[tuple[str, str, str]]:
    return [(a, pid, b) for a, b in zip(qids, qids[1:])]


@pytest.fixture
def weak_vs_strong():
    """A-B via a shared university (2 hops, weak) vs. A-C-D-B via family (3 hops, strong)."""
    return graph(
        [person(A), person(B), person(C), person(D), org(U, "University")],
        [(A, "P69", U), (B, "P69", U), (A, "P26", C), (C, "P3373", D), (D, "P26", B)],
    )


def test_shortest_fewer_edges_strongest_lower_cost(weak_vs_strong):
    cmp = compare_paths(weak_vs_strong, A, B, policy())
    short, strong = cmp.shortest.result, cmp.strongest.result
    assert short.hop_count == 2 and [n.qid for n in short.nodes] == [A, U, B]
    assert strong.hop_count == 3 and [n.qid for n in strong.nodes] == [A, C, D, B]
    assert strong.total_cost < short.total_cost
    assert not cmp.same_path


def test_cost_formula_and_breakdown(weak_vs_strong):
    p = policy()
    res = compute_shortest_path(weak_vs_strong, A, B, p).result
    first, second = res.hops
    assert first.cost.strength_cost == pytest.approx(-math.log(0.30))
    assert first.cost.hop_penalty == pytest.approx(0.20)
    # Entering the university (degree 2 in this graph) as an intermediate node is penalized...
    assert first.cost.hub_penalty == pytest.approx(0.10 * math.log1p(2))
    # ...but entering the target endpoint is not.
    assert second.cost.hub_penalty == 0
    assert res.total_cost == pytest.approx(sum(h.cost.total for h in res.hops))
    assert all(h.cost.total >= 0 for h in res.hops)


def test_paths_may_coincide():
    g = graph([person(A), person(B)], [(A, "P26", B)])
    cmp = compare_paths(g, A, B, policy())
    assert cmp.same_path
    assert cmp.shortest.result.path_id == cmp.strongest.result.path_id
    assert cmp.shortest.result.hop_count == 1


def test_parallel_edges_preserved_and_cheaper_predicate_chosen():
    g = graph([person(A), person(B)], [(A, "P3373", B), (A, "P26", B)])
    assert len(g.incident(A, frozenset({"P26", "P3373"}))) == 2
    res = compute_shortest_path(g, A, B, policy()).result
    assert res.hops[0].pid == "P26"  # spouse (0.95) beats sibling (0.85) on the tie-break by cost


def test_tie_break_is_deterministic_regardless_of_insertion_order():
    nodes = [person(A), person(B), person(X), person(Y)]
    t1 = [(A, "P3373", X), (X, "P3373", B), (A, "P3373", Y), (Y, "P3373", B)]
    r1 = compare_paths(graph(nodes, t1), A, B, policy())
    r2 = compare_paths(graph(list(reversed(nodes)), list(reversed(t1))), A, B, policy())
    assert r1.shortest.result.path_id == r2.shortest.result.path_id
    assert r1.strongest.result.path_id == r2.strongest.result.path_id


def test_reverse_traversal_keeps_original_direction():
    # A is "student of" T (= C); search from the teacher to the student.
    g = graph([person(A), person(C)], [(A, "P1066", C)])
    hop = compute_shortest_path(g, C, A, policy()).result.hops[0]
    assert (hop.from_qid, hop.to_qid) == (C, A)
    assert (hop.subject, hop.pid, hop.object) == (A, "P1066", C)
    assert hop.direction == "reverse"
    assert hop.label == "teacher of"
    assert hop.label_es == "maestro/a de"


def test_disconnected_complete_graph_reports_certain_no_path():
    g = graph([person(A), person(B), person(C)], [(A, "P26", C)], complete=True)
    cmp = compare_paths(g, A, B, policy())
    assert cmp.shortest.status == cmp.strongest.status == "no_path_within_bounds"
    assert cmp.shortest.result is None and cmp.strongest.result is None
    assert cmp.scope == "complete_configured_scope"


def test_explored_graph_never_claims_completeness(weak_vs_strong):
    cmp = compare_paths(weak_vs_strong, A, B, policy())  # InMemoryGraph(complete=False)
    assert cmp.scope == "explored_graph"
    assert cmp.shortest.result.scope == "explored_graph"


def test_complete_graph_reports_complete_scope():
    g = graph([person(A), person(B)], [(A, "P26", B)], complete=True)
    res = compute_shortest_path(g, A, B, policy()).result
    assert res.scope == "complete_configured_scope"
    assert res.snapshot == "test"
    assert res.policy_version == policy().version


def test_hop_limit_enforced_for_shortest():
    people = [f"Q9000{i:02d}" for i in range(10, 18)]  # 8 people -> 7 hops
    g = graph([person(q) for q in people], chain(people), complete=True)
    out = compute_shortest_path(g, people[0], people[-1], policy(max_hops=6))
    assert out.status == "no_path_within_bounds" and out.result is None
    assert compute_shortest_path(g, people[0], people[-1], policy(max_hops=7)).result.hop_count == 7


def test_strongest_respects_hop_limit_and_prefers_longer_strong_chain_when_allowed():
    people = [A] + [f"Q9000{i:02d}" for i in range(20, 26)] + [B]  # 7 sibling hops A..B
    g = graph(
        [person(q) for q in people] + [org(U)],
        chain(people) + [(A, "P69", U), (B, "P69", U)],
    )
    within_6 = compute_strongest_path(g, A, B, policy(max_hops=6)).result
    assert [n.qid for n in within_6.nodes] == [A, U, B]
    within_7 = compute_strongest_path(g, A, B, policy(max_hops=7)).result
    assert within_7.hop_count == 7  # 7 x sibling (~2.54) beats the university path (~2.92)


def test_hub_cannot_be_intermediate_but_can_be_endpoint():
    members = [f"Q9001{i:02d}" for i in range(5)]
    g = graph(
        [person(A), person(B), org(U)] + [person(m) for m in members],
        [(A, "P69", U), (B, "P69", U)] + [(m, "P69", U) for m in members],
    )
    p = policy(max_intermediate_degree=3)  # U has degree 7
    assert compute_shortest_path(g, A, B, p).status == "no_path_within_bounds"
    assert compute_strongest_path(g, A, B, p).status == "no_path_within_bounds"
    assert compute_shortest_path(g, A, U, p).result.hop_count == 1
    assert compute_shortest_path(g, A, B, policy(max_intermediate_degree=None)).result.hop_count == 2


def test_hub_penalty_prefers_smaller_institution():
    big_members = [f"Q9002{i:02d}" for i in range(40)]
    big, small = U, "Q910003"
    g = graph(
        [person(A), person(B), org(big), org(small)] + [person(m) for m in big_members],
        [(A, "P69", big), (B, "P69", big), (A, "P69", small), (B, "P69", small)]
        + [(m, "P69", big) for m in big_members],
    )
    res = compute_strongest_path(g, A, B, policy()).result
    assert res.nodes[1].qid == small
    assert compute_shortest_path(g, A, B, policy()).result.nodes[1].qid == small  # cost tie-break


def test_position_group_disabled_by_default():
    g = graph([person(A), person(B), Node(R, "Seat", "Escaño", "position")], [(A, "P39", R), (B, "P39", R)])
    assert compute_shortest_path(g, A, B, policy()).status == "no_path_within_bounds"
    groups = policy().default_groups | {"position"}
    res = compute_shortest_path(g, A, B, policy(), groups=groups).result
    assert res.hop_count == 2 and "position" in res.enabled_groups


def test_unknown_group_rejected():
    g = graph([person(A), person(B)], [(A, "P26", B)])
    with pytest.raises(ValueError):
        compute_shortest_path(g, A, B, policy(), groups={"nonsense"})


def test_cycles_do_not_break_search():
    g = graph(
        [person(q) for q in (A, B, C, D, E)],
        [(A, "P3373", C), (C, "P3373", D), (D, "P3373", A), (D, "P26", E), (E, "P26", B)],
    )
    cmp = compare_paths(g, A, B, policy())
    assert [n.qid for n in cmp.shortest.result.nodes] == [A, D, E, B]
    assert len({n.qid for n in cmp.strongest.result.nodes}) == cmp.strongest.result.hop_count + 1


def test_budget_exhaustion_is_partial_even_on_complete_graph():
    people = [f"Q9003{i:02d}" for i in range(30)]
    g = graph(
        [person(A), person(B)] + [person(q) for q in people],
        [(A, "P3373", q) for q in people] + [(people[-1], "P26", B)],
        complete=True,
    )
    out = compute_shortest_path(g, A, B, policy(max_visited_nodes=5))
    assert out.status == "budget_exhausted"
    assert out.scope == "explored_graph"
    assert "max_visited_nodes" in out.stats.truncated_reason
    path = [A] + [f"Q9004{i:02d}" for i in range(4)] + [B]  # 5 hops: cannot finish in 3 states
    g2 = graph([person(q) for q in path], chain(path), complete=True)
    strong = compute_strongest_path(g2, A, B, policy(max_expanded_states=3))
    assert strong.status == "budget_exhausted" and strong.scope == "explored_graph"
    assert strong.result is None


def test_strongest_with_upper_bound_still_finds_equal_cost_path():
    g = graph([person(A), person(B)], [(A, "P26", B)])
    short = compute_shortest_path(g, A, B, policy()).result
    strong = compute_strongest_path(g, A, B, policy(), upper_bound=short.total_cost).result
    assert strong.path_id == short.path_id


def test_invalid_endpoints_rejected():
    g = graph([person(A)], [])
    with pytest.raises(ValueError):
        compute_shortest_path(g, A, A, policy())
    with pytest.raises(ValueError):
        compute_shortest_path(g, A, "not-a-qid", policy())


def test_edge_ids_are_stable_and_source_sensitive():
    e1 = Edge(A, "P26", B, SRC)
    assert e1.edge_id == Edge(A, "P26", B, SRC).edge_id
    assert e1.edge_id != Edge(A, "P26", B, "other-source").edge_id
    assert e1.edge_id != Edge(B, "P26", A, SRC).edge_id
    with pytest.raises(ValueError):
        Edge(A, "P26", A, SRC)
