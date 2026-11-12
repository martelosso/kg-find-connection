"""Deterministic compact Turtle for the visible/selected part of the graph, with source spans.

RDFLib's serializer does not guarantee layout or positions, so this module writes a small,
predictable subset of Turtle itself (prefixed names, one triple per line, language-tagged
literals) and records the exact span of every statement. RDFLib is used to VALIDATE the output.

Spans are offsets in **UTF-16 code units**, which is how the browser (CodeMirror / JS strings)
indexes text; Python code-point offsets would drift after any non-BMP character.

Layout (stable for a given set of nodes/edges, independent of input order):

    @prefix ...
    # ── Entities ──
    wd:Q7186 rdfs:label "Marie Curie"@en .
    wd:Q7186 rdfs:label "Marie Curie"@es .
    # ── Wikidata statements (truthy) ──
    wd:Q7186 wdt:P26 wd:Q37463 .  # spouse
    # ── Sandbox edits (presenter overlay, NOT Wikidata) ──
    wd:Q1 sbx:P26 wd:Q2 .  # spouse
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, ConfigDict
from rdflib import Graph

from ..config import RelationshipPolicy
from .model import Edge, Node

PREFIXES = {
    "wd": "http://www.wikidata.org/entity/",
    "wdt": "http://www.wikidata.org/prop/direct/",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "sbx": "https://example.org/six-degrees/sandbox/prop/",
}


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)
    start: int  # UTF-16 code units, inclusive
    end: int  # UTF-16 code units, exclusive


class TurtleProjection(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    edge_spans: dict[str, Span]  # edge_id -> statement span
    node_spans: dict[str, list[Span]]  # qid -> label statement spans
    triple_count: int


def _qkey(qid: str) -> int:
    return int(qid[1:])


def _pkey(pid: str) -> int:
    return int(pid[1:])


def _escape(text: str) -> str:
    out = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return out


def _comment(text: str) -> str:
    return " ".join(text.split())  # comments must stay on one line


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class _Writer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0  # UTF-16 offset of the end of the text so far

    def line(self, text: str = "") -> Span:
        start = self.pos
        self.parts.append(text + "\n")
        self.pos += _u16(text) + 1
        return Span(start=start, end=start + _u16(text))

    def statement(self, stmt: str, comment: str | None = None) -> Span:
        start = self.pos
        full = f"{stmt}  # {_comment(comment)}" if comment else stmt
        self.line(full)
        return Span(start=start, end=start + _u16(stmt))


def serialize_visible_turtle(
    nodes: Iterable[Node],
    edges: Iterable[Edge],
    policy: RelationshipPolicy | None = None,
    *,
    snapshot: str | None = None,
    validate: bool = True,
) -> TurtleProjection:
    """Serialize the given nodes and edges. Edge endpoints without a Node record get no label."""
    node_map = {n.qid: n for n in nodes}
    edge_list = sorted({e.edge_id: e for e in edges}.values(),
                       key=lambda e: (e.origin != "wikidata", _qkey(e.subject), _pkey(e.pid), _qkey(e.object)))
    qids = sorted(set(node_map) | {q for e in edge_list for q in (e.subject, e.object)}, key=_qkey)

    w = _Writer()
    for prefix, iri in PREFIXES.items():
        w.line(f"@prefix {prefix}: <{iri}> .")
    w.line()
    if snapshot:
        w.line(f"# Wikidata truthy snapshot {snapshot}. Statements are what Wikidata records, not verified facts.")
        w.line()

    node_spans: dict[str, list[Span]] = {}
    triples = 0
    labelled = [q for q in qids if q in node_map and (node_map[q].label_en or node_map[q].label_es)]
    if labelled:
        w.line("# ── Entities ──")
        for qid in labelled:
            node = node_map[qid]
            for lang, label in (("en", node.label_en), ("es", node.label_es)):
                if label:
                    span = w.statement(f'wd:{qid} rdfs:label "{_escape(label)}"@{lang} .')
                    node_spans.setdefault(qid, []).append(span)
                    triples += 1
        w.line()

    def describe(e: Edge) -> str:
        label = policy.properties[e.pid].label if policy and e.pid in policy.properties else e.pid
        s = node_map.get(e.subject)
        o = node_map.get(e.object)
        return f"{s.label() if s else e.subject} — {label} → {o.label() if o else e.object}"

    edge_spans: dict[str, Span] = {}
    sections = (
        ("wikidata", "# ── Wikidata statements (truthy) ──", "wdt"),
        ("sandbox", "# ── Sandbox edits (presenter overlay, NOT Wikidata) ──", "sbx"),
    )
    for origin, header, prefix in sections:
        group = [e for e in edge_list if e.origin == origin]
        if not group:
            continue
        w.line(header)
        for e in group:
            edge_spans[e.edge_id] = w.statement(f"wd:{e.subject} {prefix}:{e.pid} wd:{e.object} .", describe(e))
            triples += 1
        w.line()

    text = "".join(w.parts)
    if validate:
        parsed = Graph().parse(data=text, format="turtle")
        if len(parsed) != triples:
            raise AssertionError(f"projection produced {len(parsed)} triples, expected {triples}")
    return TurtleProjection(text=text, edge_spans=edge_spans, node_spans=node_spans, triple_count=triples)


def slice_utf16(text: str, span: Span) -> str:
    """Return the substring of ``text`` covered by a UTF-16 span (test/debug helper)."""
    encoded = text.encode("utf-16-le")
    return encoded[span.start * 2 : span.end * 2].decode("utf-16-le")
