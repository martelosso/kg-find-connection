"""Build the SQLite people graph from a tiny N-Triples fixture and search it via SqliteGraph."""

import importlib.util
import json

import pytest

from src.graph.paths import compare_paths, compute_shortest_path
from src.graph.store import SqliteGraph

from .graph_helpers import policy

A, B, C, D = "Q900001", "Q900002", "Q900003", "Q900004"
UNIV, SEAT = "Q910001", "Q910002"


def _load_build_module(root):
    spec = importlib.util.spec_from_file_location("build_people_graph", root / "scripts" / "build_people_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def people_db(root, tmp_path):
    build = _load_build_module(root)
    meta = tmp_path / "dump.meta.json"
    meta.write_text(json.dumps({"dump_date": "20991231", "file": "fixture"}))
    out = tmp_path / "people.sqlite"
    assert build.main([str(root / "tests" / "fixtures" / "mini_slice.nt"), "--out", str(out), "--dump-meta", str(meta)]) == 0
    g = SqliteGraph(out)
    yield g
    g.close()


def test_build_filters_and_labels(people_db):
    counts = people_db.meta["counts"]
    # Duplicate P69 collapsed; self-loop dropped; org->org P463 dropped (subject not human).
    assert counts["edges_by_pid"] == {"P1066": 1, "P26": 2, "P39": 2, "P69": 2}
    a = people_db.node(A)
    assert a.label_en == "Test Person Álvaro" and a.label_es == "Persona de prueba Álvaro"
    assert people_db.node(B).label_en == 'Test Person "B"'
    assert people_db.node(D).label_en is None and people_db.node(D).label_es == "Persona de prueba D"
    assert people_db.node(UNIV).kind == "organization"
    assert people_db.node(SEAT).kind == "position"
    assert people_db.node(A).kind == "person"
    assert people_db.node("Q123456789") is None
    assert people_db.snapshot == "20991231"


def test_incident_is_bidirectional_and_filtered(people_db):
    edges = people_db.incident(A, frozenset({"P26", "P69"}))
    assert {(e.subject, e.pid, e.object) for e in edges} == {(A, "P26", C), (C, "P26", A), (A, "P69", UNIV)}
    assert people_db.incident(A, frozenset({"P39"})) == []
    assert edges == people_db.incident(A, frozenset({"P26", "P69"}))  # stable order
    assert all(e.source == "wikidata-truthy:20991231" for e in edges)


def test_paths_over_sqlite_are_complete_scope(people_db):
    cmp = compare_paths(people_db, A, B, policy())
    assert cmp.scope == "complete_configured_scope"
    # Two 2-hop routes exist (via spouse+teacher, via the university); the cheaper one wins the
    # shortest tie-break, so shortest and strongest coincide.
    assert [n.qid for n in cmp.shortest.result.nodes] == [A, C, B]
    assert cmp.same_path
    assert cmp.strongest.result.hops[1].label == "student of"
    only_institutional = compute_shortest_path(people_db, A, B, policy(), groups={"institutional"}).result
    assert [n.qid for n in only_institutional.nodes] == [A, UNIV, B]


def test_position_group_only_when_enabled(people_db):
    assert compute_shortest_path(people_db, D, B, policy()).status == "no_path_within_bounds"
    groups = policy().default_groups | {"position"}
    res = compute_shortest_path(people_db, D, B, policy(), groups=groups).result
    assert [n.qid for n in res.nodes] == [D, SEAT, B]
