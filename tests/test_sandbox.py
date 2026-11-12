"""Sandbox overlay: hypothetical sbx: edges, read-only Wikidata facts, separate path results."""

import pytest
from rdflib import Graph

from src.config import load_policy, load_search_config
from src.investigations.service import DemoCases, InvestigationService
from src.wikidata.offline import SqlitePeopleGraphProvider

from .graph_helpers import build_people_db
from .test_investigation import A, B, F, ORGS, PEOPLE, TRIPLES

C, D, H = "Q900003", "Q900004", "Q900008"
PREFIXES = """@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix sbx: <https://example.org/six-degrees/sandbox/prop/> .
"""


@pytest.fixture(scope="module")
def db_path(tmp_path_factory, request):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("sbx"), PEOPLE, ORGS, TRIPLES)


@pytest.fixture
def svc(db_path):
    provider = SqlitePeopleGraphProvider(db_path)
    yield InvestigationService(provider, load_policy(), load_search_config(), demo_cases=DemoCases(version=1),
                               chat_factory=lambda: None, agent_available=lambda: False)
    provider.close()


@pytest.fixture
def inv(svc):
    state = svc.start({"source_qid": A, "target_qid": B}, background=False)
    assert state.status == "completed"
    return state


def test_sandbox_edge_creates_a_separate_hypothetical_path(svc, inv):
    before = inv.comparison
    res = svc.set_overlay(inv.investigation_id, PREFIXES + f"wd:{A} sbx:P26 wd:{B} .\n")
    assert [e["predicate"] for e in res["sandbox_edges"]] == ["P26"]
    assert res["sandbox_edges"][0]["origin"] == "sandbox" and res["sandbox_edges"][0]["source"] == "sandbox"
    hop = res["comparison"]["shortest"]["result"]["hops"][0]
    assert res["comparison"]["shortest"]["result"]["hop_count"] == 1 and hop["origin"] == "sandbox"
    # The real (Wikidata) result and the explored graph are untouched.
    assert inv.comparison == before
    assert all(e.origin == "wikidata" for e in inv.graph.edges)


def test_turtle_puts_sandbox_edges_in_their_own_namespace(svc, inv):
    svc.set_overlay(inv.investigation_id, PREFIXES + f"wd:{A} sbx:P26 wd:{B} .\n")
    proj = svc.turtle(inv.investigation_id)
    assert f"wd:{A} sbx:P26 wd:{B} ." in proj.text
    g = Graph().parse(data=proj.text, format="turtle")
    assert len(g) == proj.triple_count
    assert "Sandbox edits" in proj.text


def test_wikidata_statements_are_read_only(svc, inv):
    text = svc.turtle(inv.investigation_id).text
    first_wdt = next(line for line in text.splitlines() if line.startswith("wd:") and " wdt:" in line)
    edited = text.replace(first_wdt, "") + f"wd:{A} wdt:P22 wd:{B} .\n"
    res = svc.set_overlay(inv.investigation_id, edited)
    assert res["sandbox_edges"] == []
    joined = " ".join(res["warnings"])
    assert "added wdt:" in joined and "removed wdt:" in joined and "read-only" in joined
    assert len(inv.graph.edges) == len(svc.get(inv.investigation_id).graph.edges)


@pytest.mark.parametrize("line,reason", [
    (f"wd:{A} sbx:P26 wd:Q123456 .", "not in the explored graph"),
    (f"wd:{A} sbx:P31 wd:{B} .", "not an allowlisted"),
    (f"wd:{A} sbx:P39 wd:{B} .", "not enabled"),
    (f"wd:{A} sbx:P26 wd:{A} .", "two different QIDs"),
])
def test_invalid_sandbox_edges_are_rejected_with_reasons(svc, inv, line, reason):
    res = svc.set_overlay(inv.investigation_id, PREFIXES + line + "\n")
    assert res["sandbox_edges"] == [] and any(reason in w for w in res["warnings"]), res["warnings"]


def test_invalid_turtle_and_clear(svc, inv, app):
    from src.investigations.service import ServiceError

    with pytest.raises(ServiceError) as exc:
        svc.set_overlay(inv.investigation_id, "wd:Q1 sbx:P26 .")
    assert exc.value.code == "invalid_turtle"
    svc.set_overlay(inv.investigation_id, PREFIXES + f"wd:{A} sbx:P26 wd:{B} .\n")
    assert svc.clear_overlay(inv.investigation_id)["sandbox_edges"] == []
    assert svc.get(inv.investigation_id).sandbox_comparison is None


def test_overlay_routes(svc, inv, app):
    app.config["INVESTIGATIONS"] = svc
    try:
        c = app.test_client()
        url = f"/api/investigations/{inv.investigation_id}/overlay"
        assert c.post(url, json={}).status_code == 400
        res = c.post(url, json={"text": PREFIXES + f"wd:{A} sbx:P3373 wd:{B} .\n"}).get_json()
        assert res["sandbox_edges"][0]["label"] == "sibling"
        assert c.get(url).get_json()["sandbox_edges"]
        ttl = c.post(f"/api/investigations/{inv.investigation_id}/turtle", json={"edge_ids": []}).get_json()
        assert f"wd:{A} sbx:P3373 wd:{B} ." in ttl["text"]  # sandbox kept even for an empty selection
        assert c.delete(url).get_json()["sandbox_edges"] == []
    finally:
        app.config.pop("INVESTIGATIONS", None)


def test_edges_not_shown_in_the_editor_are_not_reported_as_removed(svc, inv):
    shown = [e.edge_id for e in inv.graph.edges][:2]
    text = svc.turtle(inv.investigation_id, shown).text + f"wd:{A} sbx:P26 wd:{B} .\n"
    res = svc.set_overlay(inv.investigation_id, text, shown)
    assert res["warnings"] == [] and len(res["sandbox_edges"]) == 1
    noisy = svc.set_overlay(inv.investigation_id, text)  # without the hint, hidden edges look removed
    assert any("removed wdt:" in w for w in noisy["warnings"])
