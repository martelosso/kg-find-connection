"""Investigation event envelope (plan §7) and a thread-safe, append-only event log.

Every event: ``{event_id, investigation_id, type, timestamp, payload}`` with ``event_id``
strictly increasing from 1. Live runs and replays use exactly this schema.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict

from ..graph.model import Edge, Node

EventType = Literal[
    "investigation.started",
    "identity.resolved",
    "agent.decision",
    "tool.started",
    "tool.completed",
    "tool.leads",          # agent runs: the improvement leads a tool result handed the agent (a record only)
    "graph.expanded",
    "paths.updated",
    "investigation.completed",
    "investigation.partial",
    "investigation.failed",
]
TERMINAL_TYPES = frozenset({"investigation.completed", "investigation.partial", "investigation.failed"})


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: int
    investigation_id: str
    type: EventType
    timestamp: str
    payload: dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventLog:
    def __init__(self, investigation_id: str, now: Callable[[], str] = utc_now) -> None:
        self.investigation_id = investigation_id
        self._now = now
        self._events: list[Event] = []
        self._cond = threading.Condition()

    def emit(self, type_: EventType, payload: dict[str, Any]) -> Event:
        with self._cond:
            event = Event(event_id=len(self._events) + 1, investigation_id=self.investigation_id,
                          type=type_, timestamp=self._now(), payload=payload)
            self._events.append(event)
            self._cond.notify_all()
            return event

    @property
    def events(self) -> list[Event]:
        with self._cond:
            return list(self._events)

    @property
    def finished(self) -> bool:
        with self._cond:
            return bool(self._events) and self._events[-1].type in TERMINAL_TYPES

    def wait_after(self, last_id: int, timeout: float | None = None) -> list[Event]:
        """Events with ``event_id > last_id``; blocks up to ``timeout`` while there are none."""
        with self._cond:
            if len(self._events) <= last_id and not self.finished_locked():
                self._cond.wait(timeout)
            return self._events[last_id:]

    def finished_locked(self) -> bool:
        return bool(self._events) and self._events[-1].type in TERMINAL_TYPES


# ---- payload helpers (shared by live runs, replay reconstruction and the API) -------------------


def node_payload(node: Node) -> dict[str, Any]:
    return {"id": node.qid, "label": node.label("en"), "label_en": node.label_en, "label_es": node.label_es,
            "kind": node.kind, "degree": node.degree, "url": node.url}


def node_from_payload(d: dict[str, Any]) -> Node:
    return Node(d["id"], d.get("label_en"), d.get("label_es"), d.get("kind", "other"), d.get("degree", 0))


def edge_payload(edge: Edge, label: str | None = None, label_es: str | None = None,
                 group: str | None = None) -> dict[str, Any]:
    return {"id": edge.edge_id, "subject": edge.subject, "predicate": edge.pid, "object": edge.object,
            "label": label, "label_es": label_es, "group": group, "source": edge.source,
            "origin": edge.origin, "property_url": edge.property_url}


def edge_from_payload(d: dict[str, Any]) -> Edge:
    return Edge(d["subject"], d["predicate"], d["object"], d["source"], d.get("origin", "wikidata"), d["id"])
