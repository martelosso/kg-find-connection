"""rank_frontier: candidate selection and ordering on explored graphs."""

from src.config import load_search_config
from src.graph.frontier import rank_frontier

from .graph_helpers import A, B, C, D, U, graph, org, person, policy
from src.graph.model import Node

FRONTIER = load_search_config().frontier


def deg(node: Node, d: int) -> Node:
    return Node(node.qid, node.label_en, node.label_es, node.kind, d)


def test_initially_both_endpoints_are_candidates():
    g = graph([person(A), person(B)], [])
    cands = rank_frontier(g, A, B, set(), policy(), FRONTIER)
    assert {(c.qid, c.side) for c in cands} == {(A, 0), (B, 1)}


def test_only_reached_unexpanded_nodes_ranked_by_cost_and_side():
    # A expanded: spouse C (strong) and university U (weak); B expanded: sibling D.
    g = graph([person(A), person(B), deg(person(C), 5), deg(person(D), 5), deg(org(U), 50)],
              [(A, "P26", C), (A, "P69", U), (B, "P3373", D)])
    cands = rank_frontier(g, A, B, {A, B}, policy(), FRONTIER)
    assert [c.qid for c in cands][:1] == [C]  # spouse route is cheapest
    sides = {c.qid: c.side for c in cands}
    assert sides == {C: 0, U: 0, D: 1}
    assert cands == sorted(cands, key=lambda c: (c.score, c.hops, int(c.qid[1:])))
    assert all(c.via_edge_id for c in cands) and "hop" in cands[0].reason


def test_excludes_hubs_and_fully_known_nodes():
    g = graph([person(A), person(B), deg(person(C), 1), deg(org(U), 10_000)],
              [(A, "P26", C), (A, "P69", U)])
    # C's only edge is already known; U exceeds the intermediate-degree cap.
    assert rank_frontier(g, A, B, {A}, policy(), FRONTIER) == [
        c for c in rank_frontier(g, A, B, {A}, policy(), FRONTIER) if c.qid == B]
    assert [c.qid for c in rank_frontier(g, A, B, {A}, policy(max_intermediate_degree=None), FRONTIER)
            if c.qid != B] == [U]


def test_respects_hop_limit():
    g = graph([person(A), person(B), deg(person(C), 3), deg(person(D), 3)],
              [(A, "P26", C), (C, "P26", D)])
    cands = rank_frontier(g, A, B, {A, C}, policy(max_hops=2), FRONTIER)
    assert D not in {c.qid for c in cands}  # D is 2 hops out: expanding it cannot stay within 2 hops
    assert D in {c.qid for c in rank_frontier(g, A, B, {A, C}, policy(max_hops=3), FRONTIER)}


def test_negative_degree_weight_prefers_well_connected_candidates():
    # Two equally cheap spouses: a leaf (degree 2) and a well-connected one (degree 200).
    g = graph([person(A), person(B), deg(person(C), 2), deg(person(D), 200)], [(A, "P26", C), (A, "P26", D)])
    pick = lambda dw: [c.qid for c in rank_frontier(g, A, B, {A}, policy(), FRONTIER.model_copy(
        update={"degree_weight": dw})) if c.qid in (C, D)]
    assert pick(0.15) == [C, D]
    assert pick(-0.5) == [D, C]


def test_degree_weight_is_bounded():
    import pytest
    from pydantic import ValidationError

    from src.config import FrontierPolicy

    assert FrontierPolicy(degree_weight=-0.5, expansions_after_first_path=4).degree_weight == -0.5
    with pytest.raises(ValidationError):
        FrontierPolicy(degree_weight=-5, expansions_after_first_path=4)
