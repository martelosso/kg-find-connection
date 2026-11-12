"""KnowledgeProvider contract (IMPLEMENTATION_PLAN.md §4) and the shared expansion logic.

Providers return *normalized records* (QIDs, PIDs, original direction, provenance) rather than RDF;
the backend serializes the visible part to Turtle itself.

Expansion ordering is deterministic and policy-free, so every provider truncates identically:

1. by property, in the caller's ``pid_priority`` order (others after, by PID);
2. person neighbours before non-person neighbours;
3. persons by DEScending degree (more notable first), non-persons by ASCending degree
   (smaller, more specific institutions first; huge hubs are truncated away first);
4. by edge ID.

``complete`` on a batch means: every eligible edge of every requested QID (for that property
set and direction) is in the batch. Anything else is a truncated expansion and must be shown so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..graph.model import WIKIDATA_ENTITY, Edge, Node, NodeKind, is_qid

Direction = Literal["outgoing", "incoming", "both"]
ErrorCode = Literal["invalid_request", "unknown_entity", "fixture_gap", "unavailable"]


class ProviderError(Exception):
    """Structured provider failure. Never converted into (fabricated) graph data."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class EntityRecord(_Model):
    qid: str
    label_en: str | None
    label_es: str | None
    kind: NodeKind
    degree: int
    url: str

    @classmethod
    def from_node(cls, node: Node) -> "EntityRecord":
        return cls(qid=node.qid, label_en=node.label_en, label_es=node.label_es,
                   kind=node.kind, degree=node.degree, url=node.url)

    def to_node(self) -> Node:
        return Node(self.qid, self.label_en, self.label_es, self.kind, self.degree)


class PersonMatch(EntityRecord):
    matched_label: str
    matched_lang: Literal["en", "es", "qid"]


class EdgeRecord(_Model):
    edge_id: str
    subject: str
    pid: str
    object: str
    source: str
    origin: Literal["wikidata", "sandbox"] = "wikidata"

    @classmethod
    def from_edge(cls, e: Edge) -> "EdgeRecord":
        return cls(edge_id=e.edge_id, subject=e.subject, pid=e.pid, object=e.object,
                   source=e.source, origin=e.origin)

    def to_edge(self) -> Edge:
        return Edge(self.subject, self.pid, self.object, self.source, self.origin, self.edge_id)


class ExpansionRequest(_Model):
    qids: list[str] = Field(min_length=1, max_length=50)
    properties: list[str] = Field(min_length=1)
    direction: Direction
    max_edges: int = Field(ge=1, le=10_000)

    @field_validator("qids")
    @classmethod
    def _qids(cls, v: list[str]) -> list[str]:
        bad = [q for q in v if not is_qid(q)]
        if bad:
            raise ValueError(f"not QIDs: {bad}")
        return list(dict.fromkeys(v))

    @field_validator("properties")
    @classmethod
    def _pids(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not (p.startswith("P") and p[1:].isdigit())]
        if bad:
            raise ValueError(f"not PIDs: {bad}")
        return sorted(set(v), key=lambda p: int(p[1:]))


class QidCoverage(_Model):
    qid: str
    returned: int
    complete: bool  # every eligible edge of this QID is in the batch


class ExpansionBatch(_Model):
    request: ExpansionRequest
    provider: str
    snapshot: str | None
    edges: list[EdgeRecord]
    nodes: list[EntityRecord]  # every endpoint of every returned edge, plus the requested QIDs
    coverage: list[QidCoverage]
    truncated: bool
    complete: bool
    source_urls: list[str]


class KnowledgeProvider(Protocol):
    name: str

    @property
    def snapshot(self) -> str | None: ...

    def search_people(self, text: str, limit: int = 8) -> list[PersonMatch]: ...

    def get_entities(self, qids: list[str]) -> list[EntityRecord]: ...

    def expand(
        self,
        qids: list[str],
        allowed_properties: set[str] | frozenset[str],
        direction: Direction,
        max_edges: int,
        *,
        pid_priority: Sequence[str] | None = None,
    ) -> ExpansionBatch: ...


# ---- shared expansion machinery -------------------------------------------------------------


def neighbour_key(edge: Edge, neighbour: Node) -> tuple:
    """Order within one property (see module docstring, rules 2-4)."""
    if neighbour.kind == "person":
        return (0, -neighbour.degree, edge.edge_id)
    return (1, neighbour.degree, edge.edge_id)


@dataclass(frozen=True)
class RowList:
    """Edges of one QID for one (property, direction), sorted by ``neighbour_key``.

    ``complete=False`` means the list is a stored PREFIX of the full ordered list (fixtures).
    """

    items: tuple[tuple[Edge, Node], ...]
    complete: bool = True


def merge_rows(lists: Iterable[RowList]) -> RowList:
    """Merge sorted row lists; an incomplete input limits the result to its known prefix."""
    lists = list(lists)
    merged = sorted({e.edge_id: (e, n) for rl in lists for e, n in rl.items}.values(),
                    key=lambda en: neighbour_key(*en))
    capped = [rl for rl in lists if not rl.complete]
    if not capped:
        return RowList(tuple(merged), True)
    # Items beyond the smallest last key of any capped list may be missing items before them.
    limit = min((neighbour_key(*rl.items[-1]) for rl in capped if rl.items), default=None)
    if limit is None:
        return RowList((), False)
    return RowList(tuple(en for en in merged if neighbour_key(*en) <= limit), False)


class NeighbourhoodProvider:
    """Implements ``expand`` on top of two primitives a concrete provider supplies."""

    name = "abstract"

    @property
    def snapshot(self) -> str | None:  # pragma: no cover - overridden
        return None

    def source_urls(self, qids: list[str]) -> list[str]:
        return [WIKIDATA_ENTITY + q for q in qids]

    def _node(self, qid: str) -> Node | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _rows(self, qid: str, pid: str, direction: Literal["outgoing", "incoming"]) -> RowList:
        raise NotImplementedError  # pragma: no cover - abstract

    def get_entities(self, qids: list[str]) -> list[EntityRecord]:
        out = []
        for q in dict.fromkeys(qids):
            if not is_qid(q):
                raise ProviderError("invalid_request", f"not a QID: {q!r}")
            node = self._node(q)
            if node is not None:
                out.append(EntityRecord.from_node(node))
        return out

    def _ordered(self, qid: str, pids: list[str], direction: Direction) -> RowList:
        dirs: tuple[Literal["outgoing", "incoming"], ...] = (
            ("outgoing", "incoming") if direction == "both" else (direction,)
        )
        items: list[tuple[Edge, Node]] = []
        for pid in pids:
            part = merge_rows(self._rows(qid, pid, d) for d in dirs)
            items.extend(part.items)
            if not part.complete:
                return RowList(tuple(items), False)
        return RowList(tuple(items), True)

    def expand(
        self,
        qids: list[str],
        allowed_properties: set[str] | frozenset[str],
        direction: Direction,
        max_edges: int,
        *,
        pid_priority: Sequence[str] | None = None,
    ) -> ExpansionBatch:
        try:
            req = ExpansionRequest(qids=list(qids), properties=sorted(allowed_properties),
                                   direction=direction, max_edges=max_edges)
        except ValueError as exc:
            raise ProviderError("invalid_request", str(exc)) from exc
        prio = [p for p in (pid_priority or []) if p in req.properties]
        pids = prio + [p for p in req.properties if p not in prio]

        lists: dict[str, RowList] = {}
        for q in req.qids:
            if self._node(q) is None:
                raise ProviderError("unknown_entity", f"{q} is not in the {self.name} store")
            lists[q] = self._ordered(q, pids, req.direction)

        # Round-robin across requested QIDs so no QID starves the others.
        taken: dict[str, int] = {q: 0 for q in req.qids}
        chosen: dict[str, tuple[Edge, Node]] = {}
        budget = req.max_edges
        progress = True
        while budget > 0 and progress:
            progress = False
            for q in req.qids:
                items = lists[q].items
                while taken[q] < len(items) and items[taken[q]][0].edge_id in chosen:
                    taken[q] += 1  # an edge between two requested QIDs, already taken
                if budget > 0 and taken[q] < len(items):
                    edge, node = items[taken[q]]
                    chosen[edge.edge_id] = (edge, node)
                    taken[q] += 1
                    budget -= 1
                    progress = True

        coverage = []
        for q in req.qids:
            rl = lists[q]
            exhausted = taken[q] >= len(rl.items)
            if exhausted and not rl.complete and budget > 0:
                raise ProviderError(
                    "fixture_gap",
                    f"stored neighbourhood of {q} is too short to answer max_edges={req.max_edges}",
                )
            coverage.append(QidCoverage(qid=q, returned=sum(1 for e, _ in rl.items if e.edge_id in chosen),
                                        complete=exhausted and rl.complete))

        nodes: dict[str, Node] = {}
        for q in req.qids:
            nodes[q] = self._node(q)  # type: ignore[assignment]
        for edge, _ in chosen.values():
            for q in (edge.subject, edge.object):
                if q not in nodes:
                    node = self._node(q)
                    if node is None:
                        raise ProviderError("unavailable", f"edge endpoint {q} has no entity record")
                    nodes[q] = node
        truncated = not all(c.complete for c in coverage)
        return ExpansionBatch(
            request=req,
            provider=self.name,
            snapshot=self.snapshot,
            edges=[EdgeRecord.from_edge(e) for e, _ in chosen.values()],
            nodes=[EntityRecord.from_node(n) for n in sorted(nodes.values(), key=lambda n: int(n.qid[1:]))],
            coverage=coverage,
            truncated=truncated,
            complete=not truncated,
            source_urls=self.source_urls(req.qids),
        )
