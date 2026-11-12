"""Per-investigation state (plan §7). Nothing here is module-global; each run owns its graph."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from ..config import SearchBudgets
from ..graph.store import InMemoryGraph
from ..models import PathComparison
from .events import EventLog, utc_now

Status = Literal["pending", "running", "completed", "partial", "failed"]
RunMode = Literal["live", "replay"]
Strategy = Literal["deterministic", "agent"]


@dataclass(frozen=True)
class ExpansionRecord:
    """Which entity x property set x direction was searched, and whether it was complete."""

    qid: str
    side: int
    properties: tuple[str, ...]
    direction: str
    returned: int
    complete: bool
    added_nodes: int
    added_edges: int
    error: str | None = None


@dataclass
class InvestigationState:
    source_qid: str
    target_qid: str
    groups: frozenset[str]
    budgets: SearchBudgets
    strategy: Strategy = "deterministic"
    run_mode: RunMode = "live"
    snapshot: str | None = None
    investigation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Status = "pending"
    graph: InMemoryGraph = field(init=False)
    expansions: list[ExpansionRecord] = field(default_factory=list)
    provider_requests: int = 0
    tool_calls: int = 0  # agent tool calls (all tools)
    comparison: PathComparison | None = None
    stop_reason: str | None = None
    created_at: str = field(default_factory=utc_now)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: EventLog = field(init=False)
    _t0: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.graph = InMemoryGraph(complete=False, snapshot=self.snapshot)
        self.events = EventLog(self.investigation_id)

    @property
    def expanded(self) -> set[str]:
        return {r.qid for r in self.expansions}

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def cancel(self) -> None:
        self.cancel_event.set()

    def summary(self) -> dict:
        return {
            "investigation_id": self.investigation_id,
            "source_qid": self.source_qid,
            "target_qid": self.target_qid,
            "strategy": self.strategy,
            "run_mode": self.run_mode,
            "snapshot": self.snapshot,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "groups": sorted(self.groups),
            "budgets": self.budgets.model_dump(),
            "provider_requests": self.provider_requests,
            "tool_calls": self.tool_calls,
            "explored_nodes": len(self.graph.nodes),
            "explored_edges": len(self.graph.edges),
            "expanded_nodes": len(self.expansions),
            "created_at": self.created_at,
            "event_count": len(self.events.events),
        }
