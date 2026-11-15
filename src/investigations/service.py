"""In-process investigation registry: start live/replay runs in background threads, look up, cancel.

Provider choice (``provider_from_env``):
1. ``PEOPLE_GRAPH_PATH`` or data/wikidata/people_graph.sqlite -> full offline people graph;
2. otherwise the committed case fixtures in data/fixtures/cases/ (exact for recorded cases only);
3. otherwise no provider: live runs are refused with a clear error; replays still work.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..agent.client import ChatClient, chat_client_from_env, llm_settings
from ..agent.investigator import run_agent
from ..config import ROOT, RelationshipPolicy, SearchConfig, load_policy, load_search_config
from ..graph.model import is_qid
from ..wikidata.offline import FixtureProvider, SqlitePeopleGraphProvider
from ..wikidata.provider import KnowledgeProvider
from .baseline import run_deterministic
from .recording import RecordError, default_title, prepare_recording, save_demo_case
from ..graph.turtle_projection import TurtleProjection, serialize_visible_turtle
from .replay import load_trace, replay_into
from .sandbox import OverlayError, parse_overlay, sandbox_comparison
from .state import InvestigationState

DEFAULT_GRAPH_PATH = ROOT / "data" / "wikidata" / "people_graph.sqlite"
CASES_DIR = ROOT / "data" / "fixtures" / "cases"
TRACES_DIR = ROOT / "data" / "fixtures" / "event_traces"
DEMO_CASES_PATH = Path(os.environ.get("DEMO_CASES_PATH") or ROOT / "config" / "demo_cases.yaml")


def provider_from_env() -> KnowledgeProvider | None:
    path = Path(os.environ.get("PEOPLE_GRAPH_PATH") or DEFAULT_GRAPH_PATH)
    if path.is_file():
        return SqlitePeopleGraphProvider(path)
    if CASES_DIR.is_dir() and any(CASES_DIR.glob("*.json")):
        return FixtureProvider.from_dir(CASES_DIR)
    return None


class DemoCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    title: str
    source_qid: str
    source_label: str
    target_qid: str
    target_label: str
    groups: list[str] | None = None
    fixture: str | None = None  # data/fixtures/cases/<id>.json
    trace: str | None = None  # data/fixtures/event_traces/<id>.json
    notes: str = ""
    verified_on: str | None = None  # snapshot date the pair was validated against


class DemoCases(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: int
    cases: list[DemoCase] = Field(default_factory=list)


def load_demo_cases(path: Path = DEMO_CASES_PATH) -> DemoCases:
    if not path.is_file():
        return DemoCases(version=1, cases=[])
    return DemoCases.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {"version": 1})


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_qid: str | None = None
    target_qid: str | None = None
    mode: str = "deterministic"  # "deterministic" | "agent" | "replay"
    case_id: str | None = None  # required for replay
    groups: list[str] | None = None
    budgets: dict[str, float] | None = None
    max_turns: int | None = Field(default=None, ge=1)  # agent runs: model turns (up to limits.max_agent_turns)
    replay_delay_seconds: float = Field(default=0.6, ge=0, le=5)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class InvestigationService:
    MAX_KEPT = 50

    def __init__(
        self,
        provider: KnowledgeProvider | None,
        policy: RelationshipPolicy | None = None,
        search: SearchConfig | None = None,
        demo_cases: DemoCases | None = None,
        root: Path = ROOT,
        chat_factory: Callable[[], ChatClient | None] = chat_client_from_env,
        agent_available: Callable[[], bool] = lambda: llm_settings() is not None,
    ) -> None:
        self._chat_factory = chat_factory
        self._agent_available = agent_available
        self.provider = provider
        self.policy = policy or load_policy()
        self.search = search or load_search_config()
        self.demo_cases = demo_cases if demo_cases is not None else load_demo_cases()
        self.root = root
        self.cases_path = DEMO_CASES_PATH  # where "Save as demo replay" appends (tests point it elsewhere)
        self.cases_dir, self.traces_dir = CASES_DIR, TRACES_DIR
        self._record_lock = threading.Lock()
        self._runs: OrderedDict[str, InvestigationState] = OrderedDict()
        self._lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self, body: dict[str, Any], *, background: bool = True) -> InvestigationState:
        try:
            req = StartRequest.model_validate(body)
        except ValueError as exc:
            raise ServiceError(400, "invalid_request", str(exc)) from exc
        try:
            budgets = self.search.budgets.within(req.budgets, self.search.limits)
            search = self.search.with_agent_turns(req.max_turns)
            groups = frozenset(req.groups) if req.groups is not None else self.policy.default_groups
            self.policy.active_pids(groups)  # validates group names
        except ValueError as exc:
            raise ServiceError(400, "invalid_request", str(exc)) from exc

        if req.mode == "replay":
            case = self._case(req.case_id)
            if not case.trace:
                raise ServiceError(404, "no_trace", f"demo case {case.id!r} has no recorded trace")
            trace = load_trace(self.root / case.trace)
            started = trace["events"][0].payload
            state = InvestigationState(case.source_qid, case.target_qid, frozenset(started.get("groups", [])),
                                       budgets, run_mode="replay", snapshot=started.get("snapshot"))
            return self._launch(state, lambda: replay_into(state, trace, delay_seconds=req.replay_delay_seconds),
                                background)

        if req.mode not in ("deterministic", "agent"):
            raise ServiceError(400, "unsupported_mode", f"unknown mode {req.mode!r}")
        if not (req.source_qid and req.target_qid and is_qid(req.source_qid) and is_qid(req.target_qid)):
            raise ServiceError(400, "invalid_request", "source_qid and target_qid must be QIDs")
        if self.provider is None:
            raise ServiceError(503, "provider_unavailable",
                               "no people graph or case fixtures are installed; use replay mode")
        provider = self.provider
        if req.mode == "agent":
            chat = self._chat_factory()
            if chat is None:
                raise ServiceError(503, "agent_unavailable",
                                   "No LLM API key is set (OPENROUTER_API_KEY or DEEPSEEK_API_KEY), so the AI agent is unavailable. "
                                   "Use mode 'deterministic' (labelled deterministic exploration) or a replay.")
            state = InvestigationState(req.source_qid, req.target_qid, groups, budgets, strategy="agent",
                                       snapshot=provider.snapshot)
            return self._launch(state, lambda: run_agent(state, provider, self.policy, search, chat),
                                background)
        state = InvestigationState(req.source_qid, req.target_qid, groups, budgets, snapshot=provider.snapshot)
        return self._launch(state, lambda: run_deterministic(state, provider, self.policy, self.search), background)

    def _case(self, case_id: str | None) -> DemoCase:
        for c in self.demo_cases.cases:
            if c.id == case_id:
                return c
        raise ServiceError(404, "unknown_case", f"unknown demo case {case_id!r}")

    def _launch(self, state: InvestigationState, work, background: bool) -> InvestigationState:
        with self._lock:
            self._runs[state.investigation_id] = state
            while len(self._runs) > self.MAX_KEPT:
                self._runs.popitem(last=False)
        if background:
            threading.Thread(target=work, name=f"inv-{state.investigation_id}", daemon=True).start()
        else:
            work()
        return state

    def get(self, investigation_id: str) -> InvestigationState:
        with self._lock:
            state = self._runs.get(investigation_id)
        if state is None:
            raise ServiceError(404, "unknown_investigation", f"no investigation {investigation_id!r}")
        return state

    def cancel(self, investigation_id: str) -> InvestigationState:
        state = self.get(investigation_id)
        state.cancel()
        return state

    # ---- save as demo replay -------------------------------------------------------------------

    def record(self, investigation_id: str, case_id: str, title: str = "") -> dict[str, Any]:
        """Save a finished live run as a demo case: same checks as scripts/investigate.py --record,
        then fixture + trace are written and the entry is appended to demo_cases.yaml (all or nothing).
        The new case is immediately available as a replay."""
        state = self.get(investigation_id)
        notes = None
        if state.strategy == "agent":
            started = state.events.events[0].payload if state.events.events else {}
            settings = llm_settings()
            thinking = (settings.extra_body.get("thinking") or {}).get("type") if settings else None
            notes = (f"Recorded AI agent run (saved from the workbench; model {started.get('model') or 'unknown'}"
                     + (f", reasoning {'on' if thinking == 'enabled' else 'off'}" if thinking else "") + ").")
        elif state.strategy == "deterministic":
            notes = "Recorded deterministic exploration run (saved from the workbench)."
        try:
            with self._record_lock:
                rec = prepare_recording(state, self.provider, self.policy, self.search, case_id, title=title, notes=notes)
                save_demo_case(rec, cases_dir=self.cases_dir, traces_dir=self.traces_dir, cases_path=self.cases_path,
                               validate=load_demo_cases)
                self.demo_cases = load_demo_cases(self.cases_path)
        except RecordError as err:
            raise ServiceError(err.status, err.code, err.message) from err
        return {"case": rec.entry, "verification": rec.verification}

    def suggested_case(self, investigation_id: str) -> dict[str, str]:
        """A free case id and the conventional title for saving this run."""
        state = self.get(investigation_id)
        def slug(qid: str) -> str:
            n = state.graph.node(qid)
            words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", n.label() if n else qid)
                               .encode("ascii", "ignore").decode().lower())
            return words[-1] if len(words) > 1 and words[0] in ("sir", "lord", "lady", "saint") else (words[0] if words else qid.lower())
        base = f"{slug(state.source_qid)}-{slug(state.target_qid)}" + ("-agent" if state.strategy == "agent" else "")
        taken = {c.id for c in self.demo_cases.cases}
        case_id, k = base, 2
        while case_id in taken or (self.cases_dir / f"{case_id}.json").exists():
            case_id, k = f"{base}-{k}", k + 1
        return {"case_id": case_id, "title": default_title(state)}

    def _agent_status(self) -> dict[str, Any]:
        if self._agent_available():
            settings = llm_settings()
            return {"available": True, "provider": settings.provider if settings else None,
                    "model": settings.model if settings else None}
        return {"available": False, "reason": "no LLM API key is set (OPENROUTER_API_KEY or DEEPSEEK_API_KEY)",
                "fallback": "deterministic exploration mode (no AI), clearly labelled"}

    # ---- turtle & sandbox ----------------------------------------------------------------------

    def turtle(self, investigation_id: str, edge_ids: list[str] | None = None,
               include_sandbox: bool = True) -> TurtleProjection:
        state = self.get(investigation_id)
        edges = state.graph.edges
        if edge_ids is not None:
            wanted = set(edge_ids)
            edges = [e for e in edges if e.edge_id in wanted]
        sandbox = list(state.sandbox_edges) if include_sandbox else []
        qids = {q for e in edges + sandbox for q in (e.subject, e.object)}
        if edge_ids is None:
            qids |= {n.qid for n in state.graph.nodes}
        qids |= {state.source_qid, state.target_qid}
        nodes = [n for n in state.graph.nodes if n.qid in qids]
        return serialize_visible_turtle(nodes, edges + sandbox, self.policy, snapshot=state.snapshot)

    def set_overlay(self, investigation_id: str, text: str, shown_edge_ids: list[str] | None = None) -> dict[str, Any]:
        state = self.get(investigation_id)
        if state.status in ("pending", "running"):
            raise ServiceError(409, "investigation_running",
                               "wait for the investigation to finish before editing the sandbox")
        try:
            edges, warnings = parse_overlay(text, state, self.policy,
                                            set(shown_edge_ids) if shown_edge_ids is not None else None)
        except OverlayError as exc:
            raise ServiceError(400, "invalid_turtle", str(exc)) from exc
        state.sandbox_edges = edges
        state.sandbox_comparison = sandbox_comparison(state, edges, self.policy) if edges else None
        return self.overlay(investigation_id, warnings)

    def clear_overlay(self, investigation_id: str) -> dict[str, Any]:
        state = self.get(investigation_id)
        state.sandbox_edges, state.sandbox_comparison = [], None
        return self.overlay(investigation_id)

    def overlay(self, investigation_id: str, warnings: list[str] | None = None) -> dict[str, Any]:
        from .steps import edge_labels

        state = self.get(investigation_id)
        cmp = state.sandbox_comparison
        return {
            "sandbox_edges": [{"id": e.edge_id, "subject": e.subject, "predicate": e.pid, "object": e.object,
                               "source": e.source, "origin": e.origin, "property_url": e.property_url,
                               **edge_labels(self.policy, e.pid)} for e in state.sandbox_edges],
            "warnings": warnings or [],
            "comparison": cmp.model_dump(mode="json") if cmp else None,
            "note": "Sandbox edges are presenter hypotheses, not Wikidata statements.",
        }

    def health(self) -> dict[str, Any]:
        p = self.provider
        return {
            "status": "ok",
            "provider": None if p is None else {"name": p.name, "snapshot": p.snapshot},
            "replay_cases": [c.id for c in self.demo_cases.cases if c.trace],
            "policy_version": self.policy.version,
            "groups": [{"id": gid, "label": g.label, "label_es": g.label_es, "enabled_by_default": g.enabled_by_default}
                       for gid, g in self.policy.groups.items()],
            "ui": self.search.ui.model_dump(),
            # The Options menu: defaults, and the ceilings a run may set them up to.
            "budgets": self.search.budgets.model_dump(),
            "agent_max_turns": self.search.agent.max_turns,
            "limits": self.search.limits.model_dump(),
            "agent": self._agent_status(),
        }
