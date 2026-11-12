"""Presenter sandbox overlay: hypothetical edges kept apart from Wikidata facts (plan §2, §8).

The presenter edits the Turtle in the workbench and submits it. RDFLib parses it (the browser's
hand-written parser is only used for highlighting). Rules:

* Only ``sbx:<PID>`` statements become sandbox edges (origin "sandbox", source "sandbox"),
  between entities ALREADY in the explored graph (no invented people), with an allowlisted
  property of an enabled relation group.
* ``wdt:`` statements are read-only Wikidata facts: additions or removals in the editor are
  ignored and reported as warnings; the explored graph is never modified.
* Paths "with sandbox" are computed on a COPY of the explored graph plus the overlay, and every
  sandbox hop keeps ``origin: "sandbox"`` so the UI can mark it as not-Wikidata.
"""

from __future__ import annotations

from rdflib import Graph, URIRef
from rdflib.exceptions import ParserError

from ..config import RelationshipPolicy
from ..graph.model import Edge, is_qid
from ..graph.paths import compare_paths
from ..graph.store import InMemoryGraph
from ..graph.turtle_projection import PREFIXES
from ..models import PathComparison
from .state import InvestigationState

WD, WDT, SBX = PREFIXES["wd"], PREFIXES["wdt"], PREFIXES["sbx"]
RDFS_LABEL = URIRef(PREFIXES["rdfs"] + "label")
SANDBOX_SOURCE = "sandbox"
MAX_SANDBOX_EDGES = 25


class OverlayError(ValueError):
    pass


def _local(iri: str, ns: str) -> str | None:
    return iri[len(ns):] if iri.startswith(ns) else None


def parse_overlay(text: str, state: InvestigationState, policy: RelationshipPolicy,
                  shown_edge_ids: set[str] | None = None) -> tuple[list[Edge], list[str]]:
    """``shown_edge_ids``: the Wikidata edges the editor was showing (the visible part), so edges
    that were never in the text are not reported as removed. None = all explored edges."""
    if len(text) > 2_000_000:
        raise OverlayError("Turtle text is too large")
    try:
        g = Graph().parse(data=text, format="turtle")
    except (ParserError, SyntaxError, ValueError, Exception) as exc:  # rdflib raises several types
        raise OverlayError(f"invalid Turtle: {str(exc)[:300]}") from exc

    explored = {n.qid for n in state.graph.nodes}
    allowed_pids = policy.active_pids(state.groups)
    current = {(e.subject, e.pid, e.object) for e in state.graph.edges
               if shown_edge_ids is None or e.edge_id in shown_edge_ids}
    known = {(e.subject, e.pid, e.object) for e in state.graph.edges}
    warnings: list[str] = []
    in_text_wdt: set[tuple[str, str, str]] = set()
    sandbox: dict[str, Edge] = {}
    ignored_other = 0

    for s, p, o in g:
        p_str = str(p)
        if p == RDFS_LABEL:
            continue  # labels are display data; edits are ignored
        subj, obj = _local(str(s), WD), _local(str(o), WD) if isinstance(o, URIRef) else None
        if (pid := _local(p_str, WDT)) is not None and subj and obj:
            in_text_wdt.add((subj, pid, obj))
            continue
        pid = _local(p_str, SBX)
        if pid is None or not subj or not obj:
            ignored_other += 1
            continue
        where = f"sbx:{pid} between {subj} and {obj}"
        if not (is_qid(subj) and is_qid(obj)) or subj == obj:
            warnings.append(f"Ignored {where}: endpoints must be two different QIDs.")
        elif subj not in explored or obj not in explored:
            missing = [q for q in (subj, obj) if q not in explored]
            warnings.append(f"Ignored {where}: {', '.join(missing)} not in the explored graph (no invented people).")
        elif pid not in policy.properties:
            warnings.append(f"Ignored {where}: {pid} is not an allowlisted relationship.")
        elif pid not in allowed_pids:
            warnings.append(f"Ignored {where}: its relation group is not enabled for this investigation.")
        else:
            edge = Edge(subj, pid, obj, SANDBOX_SOURCE, origin="sandbox")
            sandbox[edge.edge_id] = edge

    if in_text_wdt:
        added, removed = in_text_wdt - known, current - in_text_wdt
        if added:
            warnings.append(f"{len(added)} added wdt: statement(s) ignored: Wikidata facts are read-only. "
                            "Use the sbx: prefix for hypothetical edges.")
        if removed:
            warnings.append(f"{len(removed)} removed wdt: statement(s) ignored: Wikidata facts are read-only "
                            "and stay in the explored graph.")
    if ignored_other:
        warnings.append(f"{ignored_other} other statement(s) ignored (only sbx:<PID> statements between entities are used).")
    edges = sorted(sandbox.values(), key=lambda e: e.edge_id)
    if len(edges) > MAX_SANDBOX_EDGES:
        warnings.append(f"Only the first {MAX_SANDBOX_EDGES} sandbox edges are used.")
        edges = edges[:MAX_SANDBOX_EDGES]
    return edges, warnings


def overlay_graph(state: InvestigationState, sandbox_edges: list[Edge]) -> InMemoryGraph:
    g = InMemoryGraph(complete=False, snapshot=state.snapshot)
    for n in state.graph.nodes:
        g.add_node(n)
    g.add_edges(state.graph.edges)
    g.add_edges(sandbox_edges)
    return g


def sandbox_comparison(state: InvestigationState, sandbox_edges: list[Edge], policy: RelationshipPolicy
                       ) -> PathComparison:
    return compare_paths(overlay_graph(state, sandbox_edges), state.source_qid, state.target_qid, policy, state.groups)
