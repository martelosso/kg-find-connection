"""Event traces: save a finished run, replay it with the same schema, rebuild its graph.

Trace format (``six-degrees-event-trace`` v1)::

    {"format": ..., "version": 1, "case_id": ..., "recorded_at": ..., "recorded_with": {...},
     "events": [<Event>, ...]}

Replay re-emits the recorded events in order under a NEW investigation id. Its
``investigation.started`` payload is marked ``run_mode: "replay"`` with ``replay_of`` and
``recorded_at``, so the UI can never present a replay as a live run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..graph.store import InMemoryGraph
from ..models import PathComparison
from .events import TERMINAL_TYPES, Event, edge_from_payload, node_from_payload, utc_now
from .state import InvestigationState

TRACE_FORMAT = "six-degrees-event-trace"


def trace_dict(state: InvestigationState, case_id: str, recorded_with: dict[str, Any]) -> dict[str, Any]:
    events = state.events.events
    if not events or events[-1].type not in TERMINAL_TYPES:
        raise ValueError("only finished investigations can be saved as traces")
    return {
        "format": TRACE_FORMAT,
        "version": 1,
        "case_id": case_id,
        "recorded_at": events[0].timestamp,
        "recorded_with": recorded_with,
        "events": [e.model_dump(mode="json") for e in events],
    }


def load_trace(path: Path | str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != TRACE_FORMAT or data.get("version") != 1:
        raise ValueError(f"{path}: not a v1 event trace")
    events = [Event.model_validate(e) for e in data["events"]]
    ids = [e.event_id for e in events]
    if ids != list(range(1, len(ids) + 1)):
        raise ValueError(f"{path}: event ids are not 1..n")
    if not events or events[-1].type not in TERMINAL_TYPES:
        raise ValueError(f"{path}: trace does not end with a terminal event")
    data["events"] = events
    return data


def reconstruct(events: list[Event]) -> tuple[InMemoryGraph, PathComparison | None]:
    """Rebuild the explored graph from event deltas and return the last recorded comparison."""
    started = next(e for e in events if e.type == "investigation.started")
    graph = InMemoryGraph(complete=False, snapshot=started.payload.get("snapshot"))
    comparison = None
    for e in events:
        for n in e.payload.get("added_nodes", []):
            graph.add_node(node_from_payload(n))
        for d in e.payload.get("added_edges", []):
            edge = edge_from_payload(d)
            recomputed = edge_from_payload({**d, "id": ""})
            if recomputed.edge_id != edge.edge_id:
                raise ValueError(f"edge id {edge.edge_id} does not match its triple")
            graph.add_edge(edge)
        if e.type == "paths.updated" or (e.type in TERMINAL_TYPES and e.payload.get("comparison")):
            comparison = PathComparison.model_validate(e.payload["comparison"])
    return graph, comparison


def replay_into(
    state: InvestigationState,
    trace: dict[str, Any],
    *,
    delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> InvestigationState:
    """Re-emit a loaded trace into ``state`` (whose run_mode must be "replay")."""
    if state.run_mode != "replay":
        raise ValueError("replay requires an investigation in replay mode")
    events: list[Event] = trace["events"]
    state.status = "running"
    for e in events:
        if state.cancel_event.is_set():
            state.status, state.stop_reason = "partial", "cancelled"
            state.events.emit("investigation.partial", {
                "outcome": "replay_cancelled", "stop_reason": "cancelled", "scope": "explored_graph",
                "comparison": state.comparison.model_dump(mode="json") if state.comparison else None,
            })
            return state
        payload = dict(e.payload)
        if e.type == "investigation.started":
            payload.update(run_mode="replay", replay_of=trace.get("case_id"), recorded_at=trace.get("recorded_at"),
                           replayed_at=utc_now())
        for n in payload.get("added_nodes", []):
            state.graph.add_node(node_from_payload(n))
        for d in payload.get("added_edges", []):
            state.graph.add_edge(edge_from_payload(d))
        if payload.get("comparison"):
            state.comparison = PathComparison.model_validate(payload["comparison"])
        state.events.emit(e.type, payload)
        if e.type == "graph.expanded" and delay_seconds > 0:
            sleep(delay_seconds)
    last = events[-1]
    state.stop_reason = last.payload.get("stop_reason")
    state.status = {"investigation.completed": "completed", "investigation.partial": "partial",
                    "investigation.failed": "failed"}[last.type]
    return state


def normalized_events(events: list[Event]) -> list[tuple[str, str]]:
    """(type, canonical payload) without run-specific fields (ids, timestamps, provider name).

    Two runs of the same investigation over the same data must produce equal lists, whichever
    provider (SQLite people graph or its recorded case fixture) served the data.
    """
    out = []
    for e in events:
        payload = {k: v for k, v in e.payload.items() if k != "provider"}
        out.append((e.type, json.dumps(payload, sort_keys=True, ensure_ascii=False)))
    return out
