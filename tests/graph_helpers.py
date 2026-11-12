"""Helpers for synthetic test graphs. QIDs in the Q9xxxxxxx range are fake test entities."""

from __future__ import annotations

from src.config import RelationshipPolicy, load_policy
from src.graph.model import Edge, Node
from src.graph.store import InMemoryGraph

SRC = "test-fixture"


def policy(**pathfinding) -> RelationshipPolicy:
    base = load_policy()
    if not pathfinding:
        return base
    return base.model_copy(update={"pathfinding": base.pathfinding.model_copy(update=pathfinding)})


def person(qid: str, label: str | None = None) -> Node:
    return Node(qid=qid, label_en=label or qid, label_es=label or qid, kind="person")


def org(qid: str, label: str | None = None, degree: int = 0) -> Node:
    return Node(qid=qid, label_en=label or qid, label_es=label or qid, kind="organization", degree=degree)


def graph(nodes: list[Node], triples: list[tuple[str, str, str]], *, complete: bool = False) -> InMemoryGraph:
    g = InMemoryGraph(complete=complete, snapshot="test")
    for n in nodes:
        g.add_node(n)
    for s, p, o in triples:
        g.add_edge(Edge(s, p, o, SRC))
    return g


# Common fake IDs
A, B, C, D, E, X, Y = "Q900001", "Q900002", "Q900003", "Q900004", "Q900005", "Q900006", "Q900007"
U, R = "Q910001", "Q910002"  # an organization and a position/role
