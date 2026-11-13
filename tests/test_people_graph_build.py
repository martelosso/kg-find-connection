"""Build the SQLite people graph from a tiny N-Triples fixture and search it via SqliteGraph."""

import importlib.util
import json

import pytest

from src.graph.paths import compare_paths, compute_shortest_path
from src.graph.store import IndexedPeopleGraph, SqliteGraph, open_people_graph

from .graph_helpers import build_people_db, policy, random_people_graph

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


ALL_PIDS = frozenset({"P26", "P22", "P25", "P3373", "P1066", "P108", "P69", "P463", "P39"})


def test_indexed_graph_answers_exactly_like_sqlite(people_db):
    indexed = IndexedPeopleGraph(people_db_path(people_db))
    try:
        assert indexed.complete and indexed.snapshot == people_db.snapshot
        qids = [A, B, C, D, UNIV, SEAT, "Q123456789", "Q1"]
        for q in qids:
            assert indexed.node(q) == people_db.node(q)
            n = people_db.node(q)
            assert indexed.node_profile(q) == (None if n is None else (n.kind, n.degree))
            for pids in (ALL_PIDS, frozenset({"P26", "P69"}), frozenset({"P39"}), frozenset()):
                assert indexed.incident(q, pids) == people_db.incident(q, pids)
                assert [e.edge_id for e in indexed.incident(q, pids)] == [e.edge_id for e in people_db.incident(q, pids)]
        for groups in (None, {"institutional"}, policy().default_groups | {"position"}):
            for s, t in [(A, B), (D, B), (B, C), (C, UNIV)]:
                assert compare_paths(indexed, s, t, policy(), groups) == compare_paths(people_db, s, t, policy(), groups)
    finally:
        indexed.close()


def people_db_path(graph):
    return graph._db.execute("PRAGMA database_list").fetchone()[2]


def test_indexed_graph_cache_is_reused_and_invalidated(people_db, tmp_path):
    path = people_db_path(people_db)
    first = IndexedPeopleGraph(path)
    assert first.cache_path.is_file()
    stamp = first.cache_path.stat().st_mtime_ns
    first.close()
    second = IndexedPeopleGraph(path)  # loads the cache, does not rewrite it
    assert second.cache_path.stat().st_mtime_ns == stamp
    assert second.incident(A, ALL_PIDS) == people_db.incident(A, ALL_PIDS)
    second.close()
    second.cache_path.write_bytes(b"not an npz")  # corrupt cache: rebuilt, never trusted
    third = IndexedPeopleGraph(path)
    assert third.incident(A, ALL_PIDS) == people_db.incident(A, ALL_PIDS)
    third.close()
    no_cache = IndexedPeopleGraph(path, cache=False)
    assert no_cache.node_profile(UNIV) == (people_db.node(UNIV).kind, people_db.node(UNIV).degree)
    no_cache.close()
    assert isinstance(open_people_graph(path, in_memory=False), SqliteGraph)


def test_indexed_graph_rejects_bad_qids(people_db):
    g = IndexedPeopleGraph(people_db_path(people_db), cache=False)
    assert g.node_profile("Q99999999999") is None and g.incident("Q99999999999", ALL_PIDS) == []
    assert g.node_profile("Qx") is None
    g.close()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_indexed_graph_paths_match_sqlite_on_random_graphs(root, tmp_path, seed):
    """Every pair, with a hub cap low enough to block some organisations: identical comparisons."""
    import itertools

    people, orgs, triples = random_people_graph(seed=seed, n_people=60, n_orgs=8, n_edges=260)
    path = build_people_db(root, tmp_path, people, orgs, triples)
    plain, indexed = SqliteGraph(path), IndexedPeopleGraph(path, cache=False)
    try:
        pol = policy(max_intermediate_degree=12)
        assert any(plain.node(o).degree > 12 for o in orgs if plain.node(o))  # the cap really bites
        pq = sorted(people)[:20]
        for groups in (None, pol.default_groups | {"position"}):
            for s, t in itertools.combinations(pq, 2):
                assert compare_paths(indexed, s, t, pol, groups) == compare_paths(plain, s, t, pol, groups)
    finally:
        plain.close()
        indexed.close()


def test_position_group_only_when_enabled(people_db):
    assert compute_shortest_path(people_db, D, B, policy()).status == "no_path_within_bounds"
    groups = policy().default_groups | {"position"}
    res = compute_shortest_path(people_db, D, B, policy(), groups=groups).result
    assert [n.qid for n in res.nodes] == [D, SEAT, B]
