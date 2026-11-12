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


# ---- synthetic people-graph SQLite (built by the real build script) ---------------------------

WD, WDT = "http://www.wikidata.org/entity/", "http://www.wikidata.org/prop/direct/"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


def write_slice(path, people: dict[str, str], others: dict[str, str], triples) -> None:
    """people/others: qid -> English label (Spanish label = 'ES ' + label)."""
    lines = []
    for qid, label in {**people, **others}.items():
        if qid in people:
            lines.append(f"<{WD}{qid}> <{WDT}P31> <{WD}Q5> .")
        lines.append(f'<{WD}{qid}> <{RDFS_LABEL}> "{label}"@en .')
        lines.append(f'<{WD}{qid}> <{RDFS_LABEL}> "ES {label}"@es .')
    for s, p, o in triples:
        lines.append(f"<{WD}{s}> <{WDT}{p}> <{WD}{o}> .")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_people_db(root, tmp_path, people, others, triples, dump_date="20991231"):
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("build_people_graph", root / "scripts" / "build_people_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    slice_path = tmp_path / "slice.nt"
    write_slice(slice_path, people, others, triples)
    meta = tmp_path / "dump.meta.json"
    meta.write_text(json.dumps({"dump_date": dump_date, "file": "synthetic"}))
    out = tmp_path / "people.sqlite"
    assert mod.main([str(slice_path), "--out", str(out), "--dump-meta", str(meta)]) == 0
    return out


def random_people_graph(seed: int, n_people: int = 40, n_orgs: int = 6, n_edges: int = 140):
    import random

    rng = random.Random(seed)
    people = {f"Q97{i:04d}": f"Person {i:02d}" for i in range(n_people)}
    orgs = {f"Q98{i:04d}": f"Org {i}" for i in range(n_orgs)}
    pq, oq = list(people), list(orgs)
    triples = set()
    while len(triples) < n_edges:
        s = rng.choice(pq)
        if rng.random() < 0.5:
            p, o = rng.choice(["P108", "P69", "P463", "P39"]), rng.choice(oq)
        else:
            p, o = rng.choice(["P26", "P22", "P25", "P3373", "P1066"]), rng.choice(pq)
        if s != o:
            triples.add((s, p, o))
    return people, orgs, sorted(triples)
