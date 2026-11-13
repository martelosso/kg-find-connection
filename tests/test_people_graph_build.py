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


ALL_PIDS = frozenset(policy().properties)


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


def test_v2_properties_are_extracted_and_classified(root, tmp_path):
    """P40/P451/P1038 between people; P54 makes its object an organisation (so the hub cap applies)."""
    people = {"Q900011": "Star", "Q900012": "Partner", "Q900013": "Kid", "Q900014": "Cousin", "Q900015": "Teammate"}
    others = {"Q910011": "Some FC"}
    triples = [("Q900011", "P451", "Q900012"), ("Q900011", "P40", "Q900013"), ("Q900011", "P1038", "Q900014"),
               ("Q900011", "P54", "Q910011"), ("Q900015", "P54", "Q910011")]
    g = SqliteGraph(build_people_db(root, tmp_path, people, others, triples))
    try:
        assert g.meta["counts"]["edges_by_pid"] == {"P1038": 1, "P40": 1, "P451": 1, "P54": 2}
        assert g.node("Q910011").kind == "organization"
        cmp = compare_paths(g, "Q900012", "Q900015", policy())
        assert [h.pid for h in cmp.strongest.result.hops] == ["P451", "P54", "P54"]
        assert cmp.strongest.result.hops[0].relation_group == "family"
        assert cmp.strongest.result.hops[2].label == "had player"
    finally:
        g.close()


def test_v3_work_and_founder_statements_point_at_people(root, tmp_path):
    """Work -> person credits and organization -> founder are kept although their subject is not
    human; the same properties between non-humans are not. Node kinds follow the properties."""
    actor1, actor2, director, singer, founder, worker = (f"Q90002{i}" for i in range(6))
    film, song, company, city, other_film, prize = "Q920001", "Q920002", "Q920003", "Q920004", "Q920005", "Q920006"
    people = {actor1: "Actor One", actor2: "Actor Two", director: "Director", singer: "Singer",
              founder: "Founder", worker: "Worker"}
    others = {film: "A Film", song: "A Song", company: "A Company", city: "A City", other_film: "Other Film",
              prize: "A Prize"}
    triples = [
        (film, "P161", actor1), (film, "P161", actor2), (film, "P57", director),   # work -> person: kept
        (song, "P175", singer), (song, "P175", actor2),                             # a "feat." credit
        (company, "P112", founder), (worker, "P108", company),                      # founder <- company <- worker
        (actor1, "P19", city), (director, "P19", city),                             # weak: birthplace
        (actor1, "P166", prize),                                                    # weak: award
        (other_film, "P161", film),                                                 # non-human object: dropped
        (film, "P108", company),                                                    # non-human subject: dropped
    ]
    g = SqliteGraph(build_people_db(root, tmp_path, people, others, triples))
    try:
        assert g.meta["counts"]["edges_by_pid"] == {"P108": 1, "P112": 1, "P161": 2, "P166": 1, "P175": 2,
                                                    "P19": 2, "P57": 1}
        kinds = {q: g.node(q).kind for q in (film, song, company, city, prize)}
        assert kinds == {film: "work", song: "work", company: "organization", city: "place", prize: "other"}
        assert g.node(other_film) is None
        cmp = compare_paths(g, actor1, singer, policy())
        assert [n.qid for n in cmp.shortest.result.nodes] == [actor1, film, actor2, song, singer]
        hops = cmp.shortest.result.hops
        assert [(h.pid, h.direction, h.label) for h in hops[:2]] == [("P161", "reverse", "acted in"),
                                                                    ("P161", "forward", "cast member")]
        assert hops[0].subject == film and hops[0].object == actor1  # original Wikidata direction kept
        founded = compare_paths(g, founder, worker, policy()).shortest.result
        assert [h.label for h in founded.hops] == ["founded", "employed"]
        # Weak groups only when enabled: same city, same prize.
        assert compare_paths(g, actor1, director, policy(), {"family"}).shortest.result is None
        born = compare_paths(g, actor1, director, policy(), {"birthplace"}).shortest.result
        assert [n.qid for n in born.nodes] == [actor1, city, director]
    finally:
        g.close()


def test_parallel_scan_ranges_give_the_same_result_as_one_range(root, tmp_path):
    """Workers scan newline-aligned byte ranges; merged in file order they must equal one scan."""
    import numpy as np

    from .graph_helpers import random_people_graph, write_slice

    build = _load_build_module(root)
    people, orgs, triples = random_people_graph(seed=4, n_people=80, n_orgs=10, n_edges=400)
    path = tmp_path / "slice.nt"
    write_slice(path, people, orgs, triples)
    size = path.stat().st_size
    one = build.assemble([build.scan_edges(str(path), 0, size)])
    for parts in (2, 3, 7, 50):
        rng = build.ranges(path, parts)
        assert rng[0][0] == 0 and rng[-1][1] == size
        assert all(a < b for a, b in rng) and all(b == a2 for (_, b), (a2, _) in zip(rng, rng[1:]))
        data = path.read_bytes()
        assert all(a == 0 or data[a - 1:a] == b"\n" for a, _ in rng)  # every range starts a line
        many = build.assemble([build.scan_edges(str(path), a, b) for a, b in rng])
        assert all(np.array_equal(x, y) for x, y in zip(one[0], many[0]))
        assert np.array_equal(one[1], many[1]) and one[2] == many[2]


def test_first_label_wins_within_and_across_chunks(root, tmp_path):
    build = _load_build_module(root)
    lines = [
        '<http://www.wikidata.org/entity/Q900001> <http://www.wikidata.org/prop/direct/P31> <http://www.wikidata.org/entity/Q5> .',
        '<http://www.wikidata.org/entity/Q900001> <http://www.wikidata.org/prop/direct/P26> <http://www.wikidata.org/entity/Q900002> .',
        '<http://www.wikidata.org/entity/Q900001> <http://www.w3.org/2000/01/rdf-schema#label> "First"@en .',
        '<http://www.wikidata.org/entity/Q900001> <http://www.w3.org/2000/01/rdf-schema#label> "Second"@en .',
        '<http://www.wikidata.org/entity/Q900002> <http://www.w3.org/2000/01/rdf-schema#label> "Tab\\there \\u00e9"@es .',
    ]
    path = tmp_path / "slice.nt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "g.sqlite"
    assert build.main([str(path), "--out", str(out), "--workers", "1"]) == 0
    g = SqliteGraph(out)
    try:
        assert g.node("Q900001").label_en == "First"
        assert g.node("Q900002").label_es == "Tab\there é" and g.node("Q900002").kind == "other"
    finally:
        g.close()
    # Across chunks the writer receives rows in file order and never overwrites a label.
    w = build.Writer(tmp_path / "w.sqlite")
    w.db.execute("INSERT INTO entity (qid, is_human, kind) VALUES (1, 1, 'person')")
    w.labels([(1, "en", "chunk one")])
    w.labels([(1, "en", "chunk two"), (1, "es", "uno")])
    assert w.db.execute("SELECT label_en, label_es FROM entity WHERE qid = 1").fetchone() == ("chunk one", "uno")
    w.finish([])


def test_extraction_script_and_builder_cover_every_policy_property(root):
    """Adding a PID to the policy without re-extracting would silently find nothing."""
    pids = set(policy().properties)
    script = (root / "scripts" / "extract_people_slice.sh").read_text(encoding="utf-8")
    props_line = next(line for line in script.splitlines() if line.startswith("PROPS=("))
    assert pids | {"P31"} == set(props_line[len("PROPS=("):-1].split())
    assert pids == set(_load_build_module(root).RELATION_PIDS)


def test_position_group_only_when_enabled(people_db):
    assert compute_shortest_path(people_db, D, B, policy()).status == "no_path_within_bounds"
    groups = policy().default_groups | {"position"}
    res = compute_shortest_path(people_db, D, B, policy(), groups=groups).result
    assert [n.qid for n in res.nodes] == [D, SEAT, B]
