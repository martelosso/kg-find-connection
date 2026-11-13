"""Offline KnowledgeProviders: SQLite people graph and exact-replay case fixtures."""

import itertools
import json
import random

import pytest

from src.graph.model import Edge
from src.wikidata.offline import FixtureProvider, SqlitePeopleGraphProvider, build_case_fixture
from src.wikidata.provider import ProviderError, neighbour_key

from .graph_helpers import build_people_db, random_people_graph

ALL_PIDS = ["P26", "P22", "P25", "P3373", "P40", "P451", "P1038", "P1066", "P108", "P69", "P463", "P54", "P39"]
STRONG_FIRST = ["P26", "P22", "P25", "P40", "P3373", "P451", "P1066", "P1038", "P108", "P54", "P69", "P463", "P39"]


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory, request):
    root = request.config.rootpath
    tmp = tmp_path_factory.mktemp("provider")
    people, orgs, triples = random_people_graph(seed=11)
    path = build_people_db(root, tmp, people, orgs, triples)
    prov = SqlitePeopleGraphProvider(path)
    yield prov, people, orgs, triples
    prov.close()


def test_expand_returns_every_incident_edge_with_original_direction(synthetic):
    prov, people, orgs, triples = synthetic
    q = max(people, key=lambda p: sum(p in (s, o) for s, _, o in triples))
    batch = prov.expand([q], set(ALL_PIDS), "both", 10_000)
    expected = {(s, p, o) for s, p, o in triples if q in (s, o)}
    assert {(e.subject, e.pid, e.object) for e in batch.edges} == expected
    assert batch.complete and not batch.truncated and batch.coverage[0].complete
    assert batch.snapshot == "20991231"
    assert all(e.source == "wikidata-truthy:20991231" for e in batch.edges)
    assert all(Edge(e.subject, e.pid, e.object, e.source).edge_id == e.edge_id for e in batch.edges)
    node_ids = {n.qid for n in batch.nodes}
    assert node_ids >= {x for e in batch.edges for x in (e.subject, e.object)}
    assert batch.source_urls == [f"https://www.wikidata.org/wiki/{q}"]


def test_direction_and_property_filters(synthetic):
    prov, people, orgs, triples = synthetic
    q = next(iter(people))
    out = prov.expand([q], {"P26", "P69"}, "outgoing", 10_000)
    assert {(e.subject, e.pid, e.object) for e in out.edges} == {
        t for t in triples if t[0] == q and t[1] in ("P26", "P69")}
    org = next(iter(orgs))
    inc = prov.expand([org], set(ALL_PIDS), "incoming", 10_000)
    assert {(e.subject, e.pid, e.object) for e in inc.edges} == {t for t in triples if t[2] == org}


def test_truncation_follows_documented_order_and_reports_incomplete(synthetic):
    prov, people, orgs, triples = synthetic
    org = max(orgs, key=lambda o: sum(o == t[2] for t in triples))
    full = prov.expand([org], set(ALL_PIDS), "both", 10_000, pid_priority=STRONG_FIRST)
    part = prov.expand([org], set(ALL_PIDS), "both", 3, pid_priority=STRONG_FIRST)
    assert [e.edge_id for e in part.edges] == [e.edge_id for e in full.edges][:3]
    assert part.truncated and not part.complete and part.coverage[0].returned == 3
    # Ordering: PID priority, then person neighbours by descending degree, then edge id.
    nodes = {n.qid: n.to_node() for n in full.nodes}
    keys = [(STRONG_FIRST.index(e.pid), neighbour_key(e.to_edge(), nodes[e.subject if e.object == org else e.object]))
            for e in full.edges]
    assert keys == sorted(keys)


def test_multi_qid_expansion_is_round_robin_and_deduplicated(synthetic):
    prov, people, orgs, triples = synthetic
    a, b = list(people)[:2]
    batch = prov.expand([a, b], set(ALL_PIDS), "both", 4)
    ids = [e.edge_id for e in batch.edges]
    assert len(ids) == len(set(ids)) <= 4
    assert {c.qid for c in batch.coverage} == {a, b}


def test_errors_are_structured(synthetic):
    prov, *_ = synthetic
    with pytest.raises(ProviderError) as exc:
        prov.expand(["Q123456789"], {"P26"}, "both", 10)
    assert exc.value.code == "unknown_entity"
    with pytest.raises(ProviderError) as exc:
        prov.expand(["not-a-qid"], {"P26"}, "both", 10)
    assert exc.value.code == "invalid_request"
    with pytest.raises(ProviderError):
        prov.expand(["Q970000"], {"P26"}, "sideways", 10)  # type: ignore[arg-type]
    assert prov.get_entities(["Q123456789"]) == []


def test_search_people_prefix_case_insensitive_bilingual_and_qid(synthetic):
    prov, people, *_ = synthetic
    hits = prov.search_people("person 0", limit=20)
    assert hits and all(h.label_en.lower().startswith("person 0") for h in hits)
    assert all(h.kind == "person" for h in hits)
    assert [h.degree for h in hits] == sorted((h.degree for h in hits), reverse=True)
    es = prov.search_people("es person 01")
    assert es[0].qid == "Q970001" and es[0].matched_lang == "es"
    assert prov.search_people("q970002")[0].qid == "Q970002"
    assert prov.search_people("Org 1") == []  # organizations are not people
    assert prov.search_people("   ") == []


def _batches_equal(a, b):
    return (a.model_dump(exclude={"provider"}) == b.model_dump(exclude={"provider"}))


def test_fixture_reproduces_sqlite_exactly_within_cap(synthetic):
    prov, people, orgs, triples = synthetic
    qids = list(people)[:6] + list(orgs)[:2]
    fixture = json.loads(json.dumps(build_case_fixture(prov, "t", qids, ALL_PIDS, cap=5)))  # JSON round-trip
    fx = FixtureProvider([fixture])
    rng = random.Random(3)
    checked = 0
    for q, direction, max_edges in itertools.product(qids, ["both", "outgoing", "incoming"], [1, 3, 5]):
        pids = set(rng.sample(ALL_PIDS, rng.randint(1, len(ALL_PIDS))))
        prio = rng.sample(ALL_PIDS, len(ALL_PIDS))
        expected = prov.expand([q], pids, direction, max_edges, pid_priority=prio)
        try:
            got = fx.expand([q], pids, direction, max_edges, pid_priority=prio)
        except ProviderError as exc:
            # Only allowed when the stored prefix is genuinely too short to decide.
            assert exc.code == "fixture_gap"
            continue
        assert _batches_equal(expected, got), (q, direction, max_edges)
        checked += 1
    assert checked > 50


def test_fixture_refuses_unrecorded_and_overlong_requests(synthetic):
    prov, people, orgs, triples = synthetic
    org = max(orgs, key=lambda o: sum(o == t[2] for t in triples))
    fx = FixtureProvider([build_case_fixture(prov, "t", [org], ALL_PIDS, cap=2)])
    with pytest.raises(ProviderError) as exc:
        fx.expand([org], set(ALL_PIDS), "both", 50)
    assert exc.value.code == "fixture_gap"
    other = next(q for q in people if not fx.covers(q) and fx._node(q) is not None)
    with pytest.raises(ProviderError) as exc:
        fx.expand([other], set(ALL_PIDS), "both", 1)
    assert exc.value.code == "fixture_gap"
    assert fx.search_people("person")  # entity records of recorded neighbours are searchable


def test_fixture_rejects_mixed_snapshots(synthetic):
    prov, people, *_ = synthetic
    a = build_case_fixture(prov, "a", [next(iter(people))], ALL_PIDS)
    b = {**a, "case_id": "b", "snapshot": "20000101"}
    with pytest.raises(ValueError):
        FixtureProvider([a, b])
