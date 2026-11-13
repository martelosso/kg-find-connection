"""Core graph records. Node IDs are Wikidata QIDs ("Q42"); predicates are PIDs ("P26").

Edges always keep their ORIGINAL Wikidata direction (subject -> object). Traversal may cross an
edge in either direction; the path layer records which.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

NodeKind = Literal["person", "organization", "work", "position", "place", "other"]
EdgeOrigin = Literal["wikidata", "sandbox"]

WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/"
WIKIDATA_PROPERTY = "https://www.wikidata.org/wiki/Property:"


def is_qid(value: str) -> bool:
    return len(value) > 1 and value[0] == "Q" and value[1:].isdigit()


def edge_id_for(subject: str, pid: str, obj: str, source: str) -> str:
    """Deterministic edge ID from (subject, predicate, object, source). Never array positions."""
    digest = hashlib.sha1(f"{subject}|{pid}|{obj}|{source}".encode()).hexdigest()
    return f"e-{digest[:16]}"


@dataclass(frozen=True, slots=True)
class Node:
    qid: str
    label_en: str | None = None
    label_es: str | None = None
    kind: NodeKind = "other"
    degree: int = 0

    def label(self, lang: str = "en") -> str:
        primary, fallback = (self.label_es, self.label_en) if lang == "es" else (self.label_en, self.label_es)
        return primary or fallback or self.qid

    @property
    def url(self) -> str:
        return WIKIDATA_ENTITY + self.qid


@dataclass(frozen=True, slots=True)
class Edge:
    subject: str
    pid: str
    object: str
    source: str  # provenance, e.g. "wikidata-truthy:20261104" or "sandbox"
    origin: EdgeOrigin = "wikidata"
    edge_id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not (is_qid(self.subject) and is_qid(self.object)):
            raise ValueError(f"edge endpoints must be QIDs: {self.subject!r}, {self.object!r}")
        if self.subject == self.object:
            raise ValueError(f"self-loop not allowed: {self.subject}")
        if not self.edge_id:
            object.__setattr__(self, "edge_id", edge_id_for(self.subject, self.pid, self.object, self.source))

    def other(self, qid: str) -> str:
        if qid == self.subject:
            return self.object
        if qid == self.object:
            return self.subject
        raise ValueError(f"{qid} is not an endpoint of {self.edge_id}")

    @property
    def property_url(self) -> str:
        return WIKIDATA_PROPERTY + self.pid
