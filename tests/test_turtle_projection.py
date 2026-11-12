"""Compact Turtle projection: validity, determinism, escaping and UTF-16 spans."""

from rdflib import Graph, Literal, URIRef

from src.config import load_policy
from src.graph.model import Edge, Node
from src.graph.turtle_projection import PREFIXES, serialize_visible_turtle, slice_utf16

from .graph_helpers import SRC

A, B, C, U = "Q900001", "Q900002", "Q900003", "Q910001"
NODES = [
    Node(A, "Ada Test", "Ada Prueba", "person"),
    Node(B, 'Bea "Quote" Test\\', None, "person"),
    Node(C, "Emoji 🎻 Test", "Prueba 🎻 ñ", "person"),
    Node(U, "Test University", "Universidad de Prueba", "organization"),
]
EDGES = [
    Edge(A, "P26", B, SRC),
    Edge(A, "P69", U, SRC),
    Edge(C, "P69", U, SRC),
    Edge(C, "P1066", A, SRC),
]


def project(nodes=NODES, edges=EDGES, **kw):
    return serialize_visible_turtle(nodes, edges, load_policy(), **kw)


def test_reparses_with_rdflib_and_contains_expected_triples():
    proj = project(snapshot="20261104")
    g = Graph().parse(data=proj.text, format="turtle")
    assert len(g) == proj.triple_count == 4 + 7  # 4 edges + 7 labels
    wd, wdt = PREFIXES["wd"], PREFIXES["wdt"]
    assert (URIRef(wd + A), URIRef(wdt + "P26"), URIRef(wd + B)) in g
    assert (URIRef(wd + B), URIRef(PREFIXES["rdfs"] + "label"), Literal('Bea "Quote" Test\\', lang="en")) in g
    assert "snapshot 20261104" in proj.text


def test_output_is_independent_of_input_order():
    assert project().text == project(list(reversed(NODES)), list(reversed(EDGES))).text


def test_edge_spans_cover_exact_statements_even_after_non_bmp_characters():
    proj = project()
    for e in EDGES:
        assert slice_utf16(proj.text, proj.edge_spans[e.edge_id]) == f"wd:{e.subject} wdt:{e.pid} wd:{e.object} ."
    # C's labels contain an emoji (2 UTF-16 units); spans after it must still be exact.
    c_spans = proj.node_spans[C]
    assert [slice_utf16(proj.text, s) for s in c_spans] == [
        'wd:Q900003 rdfs:label "Emoji 🎻 Test"@en .',
        'wd:Q900003 rdfs:label "Prueba 🎻 ñ"@es .',
    ]
    # Python code-point offsets would differ from UTF-16 offsets after the emoji:
    last = proj.edge_spans[EDGES[-1].edge_id]
    assert proj.text.find("wd:Q900003 wdt:P1066") != last.start


def test_human_readable_comments_and_sandbox_section():
    sandbox = Edge(B, "P3373", C, "sandbox", origin="sandbox")
    proj = project(edges=EDGES + [sandbox])
    assert "# Ada Test — spouse → Bea" in proj.text
    stmt = slice_utf16(proj.text, proj.edge_spans[sandbox.edge_id])
    assert stmt == "wd:Q900002 sbx:P3373 wd:Q900003 ."
    assert proj.text.index("Sandbox edits") > proj.text.index("Wikidata statements")
    g = Graph().parse(data=proj.text, format="turtle")
    assert (URIRef(PREFIXES["wd"] + B), URIRef(PREFIXES["wdt"] + "P3373"), URIRef(PREFIXES["wd"] + C)) not in g


def test_edges_without_node_records_and_empty_input():
    proj = serialize_visible_turtle([], [Edge(A, "P26", B, SRC)])
    assert proj.triple_count == 1 and not proj.node_spans
    empty = serialize_visible_turtle([], [])
    assert empty.triple_count == 0 and Graph().parse(data=empty.text, format="turtle") is not None
