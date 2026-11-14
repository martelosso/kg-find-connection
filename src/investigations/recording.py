"""Save a finished live investigation as a demo case (fixture + event trace + demo_cases.yaml entry).

Shared by ``scripts/investigate.py --record`` and the workbench's "Save as demo replay"
(``POST /api/investigations/<id>/record``), so both apply the same checks before anything is written:

* only a finished live run with a path is recorded (demo cases must show a real connection);
* deterministic runs are RE-RUN against the fixture alone and must reproduce identical events;
* agent runs cannot be re-run identically, so their trace must rebuild the same graph and paths, and
  the backend must recompute the same paths from that graph.

``save_demo_case`` also appends the entry to demo_cases.yaml (never overwriting a case) and rolls
every file back if any step fails.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import RelationshipPolicy, SearchConfig
from ..graph.paths import compare_paths
from ..wikidata.offline import FixtureProvider, SqlitePeopleGraphProvider, build_case_fixture
from .baseline import run_deterministic
from .events import Event
from .replay import normalized_events, reconstruct, trace_dict
from .state import InvestigationState

CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


class RecordError(Exception):
    """Why a run cannot be recorded. ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Recording:
    case_id: str
    fixture: dict[str, Any]
    trace: dict[str, Any]
    verification: str  # what was checked, in words
    entry: dict[str, Any]  # the demo_cases.yaml entry (field order kept)


def default_title(state: InvestigationState) -> str:
    s, t = state.graph.node(state.source_qid), state.graph.node(state.target_qid)
    names = f"{s.label() if s else state.source_qid} and {t.label() if t else state.target_qid}"
    return f"{names} (AI agent)" if state.strategy == "agent" else names


def prepare_recording(state: InvestigationState, provider: Any, policy: RelationshipPolicy,
                      search: SearchConfig, case_id: str, *, title: str = "", notes: str | None = None) -> Recording:
    """Build and verify the fixture and trace of a finished live run. Writes nothing."""
    if not CASE_ID_RE.match(case_id or ""):
        raise RecordError(400, "invalid_case_id",
                          "case id: 3-64 characters, lowercase letters, digits and hyphens (e.g. lula-snoop-agent)")
    if state.run_mode != "live":
        raise RecordError(409, "not_live", "a replay is already a recording; only live runs can be saved")
    if state.status in ("pending", "running"):
        raise RecordError(409, "investigation_running", "wait for the run to finish before saving it")
    if state.status == "failed":
        raise RecordError(409, "investigation_failed", "a failed run cannot be saved as a demo case")
    if not (state.comparison and state.comparison.shortest.result):
        raise RecordError(409, "no_path", "no path was found: demo cases must show a real connection")
    if not isinstance(provider, SqlitePeopleGraphProvider):
        raise RecordError(503, "people_graph_required", "recording needs the full people graph (PEOPLE_GRAPH_PATH)")

    expanded = [r.qid for r in state.expansions if r.error is None]
    cap = max(500, search.budgets.max_edges_per_expansion)
    fixture = build_case_fixture(provider, case_id, expanded + [state.source_qid, state.target_qid],
                                 policy.properties.keys(), cap=cap)
    started = state.events.events[0].payload
    trace = trace_dict(state, case_id, {"provider": provider.name, "snapshot": provider.snapshot,
                                        "strategy": state.strategy, "model": started.get("model"),
                                        "policy_version": policy.version, "search_version": search.version})
    if state.strategy == "deterministic":
        replay = InvestigationState(state.source_qid, state.target_qid, state.groups, state.budgets,
                                    snapshot=state.snapshot)
        run_deterministic(replay, FixtureProvider([fixture]), policy, search)
        if normalized_events(replay.events.events) != normalized_events(state.events.events):
            raise RecordError(409, "not_reproducible", "the fixture does not reproduce the run; nothing was saved")
        how = "fixture reproduces the run exactly"
    else:
        events = [Event.model_validate(e) for e in trace["events"]]
        _graph, cmp = reconstruct(events)
        recomputed = compare_paths(_graph, state.source_qid, state.target_qid, policy, state.groups)
        if cmp != state.comparison or recomputed != state.comparison:
            raise RecordError(409, "not_reproducible", "the trace does not rebuild the same paths; nothing was saved")
        how = "trace rebuilds the same graph and paths"

    s, t = state.graph.node(state.source_qid), state.graph.node(state.target_qid)
    entry: dict[str, Any] = {
        "id": case_id, "title": title.strip() or default_title(state),
        "source_qid": state.source_qid, "source_label": s.label() if s else state.source_qid,
        "target_qid": state.target_qid, "target_label": t.label() if t else state.target_qid,
    }
    if state.groups != policy.default_groups:
        entry["groups"] = sorted(state.groups)
    strategy = "AI agent" if state.strategy == "agent" else "deterministic exploration"
    entry |= {"fixture": f"data/fixtures/cases/{case_id}.json", "trace": f"data/fixtures/event_traces/{case_id}.json",
              "notes": notes if notes is not None else f"Recorded {strategy} run.",
              "verified_on": str(provider.snapshot)}
    return Recording(case_id, fixture, trace, how, entry)


def entry_yaml(entry: dict[str, Any]) -> str:
    """The entry as demo_cases.yaml lines (JSON-quoted strings are valid YAML)."""
    lines = []
    for i, (k, v) in enumerate(entry.items()):
        # ids, QIDs and paths are plain (as in the hand-checked file); free text is JSON-quoted
        value = v if k in ("id", "source_qid", "target_qid", "fixture", "trace") else json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            value = "[" + ", ".join(v) + "]"
        lines.append(f"{'  - ' if i == 0 else '    '}{k}: {value}")
    return "\n".join(lines) + "\n"


def write_recording(rec: Recording, cases_dir: Path, traces_dir: Path) -> tuple[Path, Path]:
    cases_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)
    fx_path, tr_path = cases_dir / f"{rec.case_id}.json", traces_dir / f"{rec.case_id}.json"
    fx_path.write_text(json.dumps(rec.fixture, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    tr_path.write_text(json.dumps(rec.trace, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return fx_path, tr_path


def save_demo_case(rec: Recording, *, cases_dir: Path, traces_dir: Path, cases_path: Path,
                   validate: Callable[[Path], Any]) -> None:
    """Write the fixture and trace and append the entry to demo_cases.yaml; all or nothing.

    ``validate(cases_path)`` must load and validate the updated file (it raises on a bad file). An
    existing case id, fixture or trace is never overwritten.
    """
    existing = cases_path.read_text(encoding="utf-8") if cases_path.is_file() else "version: 1\ncases:\n"
    fx_path, tr_path = cases_dir / f"{rec.case_id}.json", traces_dir / f"{rec.case_id}.json"
    if re.search(rf"^\s*-\s*id:\s*{re.escape(rec.case_id)}\s*$", existing, re.M) or fx_path.exists() or tr_path.exists():
        raise RecordError(409, "case_exists", f"a demo case named {rec.case_id!r} already exists; pick another name")
    written: list[Path] = []
    try:
        written += write_recording(rec, cases_dir, traces_dir)
        cases_path.write_text(existing.rstrip("\n") + "\n" + entry_yaml(rec.entry), encoding="utf-8", newline="\n")
        validate(cases_path)
    except Exception:
        cases_path.write_text(existing, encoding="utf-8", newline="\n")
        for p in written:
            p.unlink(missing_ok=True)
        raise
