"""The teaching ontology (ontology/people_connections.ttl): parses, agrees with the policy and the
graph builder, is served to the workbench, and its browser-side index and diagram match RDFLib."""

import importlib.util
import json

import pytest
from rdflib import Graph, URIRef

from src.config import load_policy
from src.ontology import DEFAULT_ONTOLOGY_PATH, OntologyError, load_people_ontology
from src.preflight import run_checks

from .test_ui_modules import NODE, run_node

KIND_OF_CLASS = {"Person": "person", "Organization": "organization", "Work": "work", "Place": "place",
                 "Position": "position", "Award": "other"}


@pytest.fixture(scope="module")
def view():
    return load_people_ontology(load_policy())


def builder(root):
    spec = importlib.util.spec_from_file_location("build_people_graph", root / "scripts" / "build_people_graph.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ontology_covers_exactly_the_policy(view):
    policy = load_policy()
    assert [p.pid for p in view.properties] == list(policy.properties)
    assert [g.id for g in view.groups] == list(policy.groups)
    for p in view.properties:
        pol = policy.properties[p.pid]
        assert (p.group, p.label, p.symmetric, p.strength) == (pol.group, pol.label, pol.symmetric, pol.strength)
    assert {c.id: c.kind for c in view.classes} == KIND_OF_CLASS
    assert {g.link_type for g in view.groups} == {"DirectTie", "SharedAffiliation"}
    # The association rule written as data: everything through an organization, work or place is
    # a shared affiliation, never a direct tie.
    by_group = {g.id: g for g in view.groups}
    for p in view.properties:
        if "Person" not in (p.domain, p.range) or p.domain != p.range:
            assert by_group[p.group].link_type == "SharedAffiliation", p.pid


def test_domain_and_range_agree_with_the_graph_builder(root, view):
    b = builder(root)
    props = {p.pid: p for p in view.properties}
    for pid in b.INSTITUTION_PIDS:
        assert (props[pid].domain, props[pid].range) == ("Person", "Organization"), pid
    for pid in b.WORK_PIDS:
        assert (props[pid].domain, props[pid].range) == ("Work", "Person"), pid
    assert (props["P112"].domain, props["P112"].range) == ("Organization", "Person")
    assert props["P19"].range == "Place" and props["P39"].range == "Position"
    assert set(b.RELATION_PIDS) == set(props)


def test_inverses_are_declared_both_ways_and_only_where_exact(view):
    inv = {p.pid: p.inverse_of for p in view.properties if p.inverse_of}
    assert inv == {"P1066": "P802", "P802": "P1066", "P184": "P185", "P185": "P184"}


def test_disagreement_with_the_policy_is_refused(tmp_path):
    text = DEFAULT_ONTOLOGY_PATH.read_text(encoding="utf-8")
    bad = tmp_path / "bad.ttl"
    bad.write_text(text.replace("ex:relationGroup ex:works .", "ex:relationGroup ex:family .", 1), encoding="utf-8")
    with pytest.raises(OntologyError, match="group"):
        load_people_ontology(load_policy(), bad)
    bad.write_text(text.replace("wdt:P737 a owl:ObjectProperty", "wdt:P999 a owl:ObjectProperty"), encoding="utf-8")
    with pytest.raises(OntologyError):
        load_people_ontology(load_policy(), bad)
    bad.write_text(text + "\nex:Broken a", encoding="utf-8")
    with pytest.raises(OntologyError, match="parse"):
        load_people_ontology(load_policy(), bad)


def test_endpoint_serves_the_ontology(client):
    body = client.get("/api/people-ontology").get_json()
    assert body["text"].startswith("# People connections")
    assert len(body["properties"]) == len(load_policy().properties)
    assert {"pid", "domain", "range", "group", "strength", "inverse_label"} <= set(body["properties"][0])
    # the legacy retail route is untouched
    assert client.get("/api/ontology").status_code == 200


def test_preflight_checks_the_ontology(root):
    checks = [c for c in run_checks(root, port=False) if c.name == "ontology"]
    assert len(checks) == 1 and checks[0].level == "ok", checks


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_browser_index_and_diagram_match_rdflib(root, tmp_path, view):
    data = view.model_dump()
    script = """
const { ontologyIndex, schemaGraph, schemaToSpans, selectionToSchema, ruleFor, rulesFor } = await mod('ontology.js');
const idx = ontologyIndex(data.text, data);
const schema = schemaGraph(data, ['family', 'works'], new Set(['P161', 'P57']));
const sel = schemaToSpans(idx, schema, { arrows: ['Work>Person:works'] }, data.text);
const back = selectionToSchema(idx, schema, sel.spans.slice(0, 1), data.text);
const stale = schemaToSpans(idx, schema, { arrows: ['Work>Person:works'] }, data.text + ' ');
console.log(JSON.stringify({
  blocks: idx.blocks.map((b) => ({ kind: b.kind, id: b.id, text: data.text.slice(b.span[0], b.span[1]) })),
  arrows: schema.arrows.map((a) => ({ id: a.id, enabled: a.enabled, used: a.used, pids: a.properties.map((p) => p.pid), word: a.word })),
  nodes: schema.nodes.map((n) => n.id),
  spans: sel.spans.map(([a, b]) => data.text.slice(a, b).split(' ')[0]),
  back: { classes: [...back.classes], arrows: [...back.arrows] },
  stale: stale.stale,
  rule: ruleFor(data, 'P161'),
  rules: rulesFor(data, ['P161', 'P57', 'P161']).map((r) => r.pid),
}));
"""
    out = run_node(root, tmp_path, script, data)

    # Every block the browser found is exactly one RDFLib subject's statements.
    g = Graph().parse(data=view.text, format="turtle")
    prefixes = {"class": "https://example.org/six-degrees/ontology#", "group": "https://example.org/six-degrees/ontology#",
                "property": "http://www.wikidata.org/prop/direct/"}
    for b in out["blocks"]:
        sub = Graph().parse(data="\n".join(f"@prefix {p}: <{ns}> ." for p, ns in g.namespaces()) + "\n" + b["text"],
                            format="turtle")
        subject = URIRef(prefixes[b["kind"]] + b["id"])
        assert set(sub) == set(g.triples((subject, None, None))), b["id"]
    kinds = {}
    for b in out["blocks"]:
        kinds.setdefault(b["kind"], set()).add(b["id"])
    assert kinds["property"] == {p.pid for p in view.properties}
    assert kinds["class"] == {c.id for c in view.classes}
    assert kinds["group"] == {gr.id for gr in view.groups}

    arrows = {a["id"]: a for a in out["arrows"]}
    assert sum(len(a["pids"]) for a in arrows.values()) == len(view.properties)
    assert len(arrows) == 10
    assert arrows["Work>Person:works"] == {"id": "Work>Person:works", "enabled": True, "used": True,
                                           "pids": ["P161", "P57", "P58", "P86", "P175"], "word": "medium"}
    assert arrows["Person>Person:family"]["enabled"] and not arrows["Person>Organization:institutional"]["enabled"]
    assert arrows["Person>Person:family"]["word"] == "strong" and not arrows["Person>Person:family"]["used"]
    assert sorted(out["nodes"]) == sorted(KIND_OF_CLASS)

    assert out["spans"] == ["ex:works", "wdt:P161", "wdt:P57", "wdt:P58", "wdt:P86", "wdt:P175"]
    assert out["back"] == {"classes": [], "arrows": ["Work>Person:works"]}
    assert out["stale"] is True
    assert {k: out["rule"][k] for k in ("domainLabel", "rangeLabel", "label", "groupLabel", "linkType", "arrowId")} == {
        "domainLabel": "Film / music", "rangeLabel": "Person", "label": "cast member", "groupLabel": "Shared film / music",
        "linkType": "SharedAffiliation", "arrowId": "Work>Person:works"}
    assert out["rules"] == ["P161", "P57"]


def test_view_json_is_serializable(view):
    json.dumps(view.model_dump())
