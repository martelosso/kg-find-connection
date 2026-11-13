"""Deterministic investigation, event log and replay (Milestone 3). No LLM, no network."""

import json

import pytest

from src.config import load_search_config
from src.graph.paths import compare_paths
from src.graph.store import SqliteGraph
from src.investigations.baseline import run_deterministic
from src.investigations.events import TERMINAL_TYPES
from src.investigations.replay import load_trace, normalized_events, reconstruct, replay_into, trace_dict
from src.investigations.state import InvestigationState
from src.wikidata.offline import FixtureProvider, SqlitePeopleGraphProvider, build_case_fixture

from .graph_helpers import build_people_db, policy

ALL = list(policy().properties)  # what scripts/investigate.py records: every policy property
# A=spouse=C, C sibling D, D spouse B; A and B also studied at U. F/G form a separate component.
PEOPLE = {"Q900001": "Ada", "Q900002": "Bea", "Q900003": "Cy", "Q900004": "Dee", "Q900006": "Fay",
          "Q900007": "Gus", "Q900008": "Hal"}
ORGS = {"Q910001": "Small College", "Q910002": "Seat"}
TRIPLES = [
    ("Q900001", "P26", "Q900003"), ("Q900003", "P3373", "Q900004"), ("Q900004", "P26", "Q900002"),
    ("Q900001", "P69", "Q910001"), ("Q900002", "P69", "Q910001"), ("Q900008", "P69", "Q910001"),
    ("Q900006", "P26", "Q900007"), ("Q900007", "P39", "Q910002"),
]
A, B, F, G = "Q900001", "Q900002", "Q900006", "Q900007"
SEARCH = load_search_config()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory, request):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("inv"), PEOPLE, ORGS, TRIPLES)


@pytest.fixture
def provider(db_path):
    p = SqlitePeopleGraphProvider(db_path)
    yield p
    p.close()


def run(provider, s=A, t=B, budgets=None, groups=None, pol=None, cancel=False):
    pol = pol or policy()
    state = InvestigationState(s, t, frozenset(groups or pol.default_groups),
                               SEARCH.budgets.lowered(budgets), snapshot=provider.snapshot)
    if cancel:
        state.cancel()
    return run_deterministic(state, provider, pol, SEARCH)


def comparable(state):
    return normalized_events(state.events.events)


def test_finds_both_paths_matching_the_complete_graph(provider, db_path):
    state = run(provider)
    # This graph is tiny, so the frontier may run dry before the extra post-path expansions.
    assert state.status == "completed" and state.stop_reason in ("path_found", "frontier_exhausted")
    cmp = state.comparison
    assert [n.qid for n in cmp.shortest.result.nodes] == [A, "Q910001", B]
    assert [n.qid for n in cmp.strongest.result.nodes] == [A, "Q900003", "Q900004", B]
    assert cmp.scope == "explored_graph" and not cmp.same_path
    full = compare_paths(SqliteGraph(db_path), A, B, policy())
    assert full.shortest.result.path_id == cmp.shortest.result.path_id
    assert full.strongest.result.path_id == cmp.strongest.result.path_id


def test_event_log_contract(provider):
    events = run(provider).events.events
    assert [e.event_id for e in events] == list(range(1, len(events) + 1))
    assert events[0].type == "investigation.started" and events[1].type == "identity.resolved"
    assert events[-1].type == "investigation.completed"
    assert sum(e.type in TERMINAL_TYPES for e in events) == 1
    assert len({e.investigation_id for e in events}) == 1
    started = [e.payload["step"] for e in events if e.type == "tool.started"]
    completed = [e.payload["step"] for e in events if e.type == "tool.completed"]
    assert started == completed == list(range(1, len(started) + 1))
    for e in events:
        if e.type == "graph.expanded":
            assert e.payload["highlight_edge_ids"] == [d["id"] for d in e.payload["added_edges"]]
            assert all(d["source"].startswith("wikidata-truthy:") for d in e.payload["added_edges"])
    final = events[-1].payload
    assert final["scope"] == "explored_graph" and "not verified facts" in final["note"]
    json.dumps([e.model_dump(mode="json") for e in events])  # serializable for SSE / traces


def test_runs_are_deterministic(provider):
    assert comparable(run(provider)) == comparable(run(provider))


def test_honest_no_path_when_components_are_disconnected(provider):
    state = run(provider, A, F)
    assert state.status == "completed" and state.stop_reason == "frontier_exhausted"
    final = state.events.events[-1].payload
    assert final["outcome"] == "no_path_found" and final["scope"] == "explored_graph"
    assert state.comparison is None or state.comparison.shortest.result is None


def test_budget_cap_gives_partial_with_reason(provider):
    state = run(provider, budgets={"max_provider_requests": 1})
    assert state.status == "partial" and state.stop_reason == "max_provider_requests"
    assert state.events.events[-1].type == "investigation.partial"
    assert state.provider_requests == 1


def test_node_cap_trims_batch_and_stops(provider):
    state = run(provider, budgets={"max_explored_nodes": 3})
    assert state.status == "partial" and state.stop_reason == "max_explored_nodes"
    assert len(state.graph.nodes) <= 3


def test_cancel_gives_partial(provider):
    state = run(provider, cancel=True)
    assert state.status == "partial" and state.stop_reason == "cancelled"


@pytest.mark.parametrize("s,t,code", [(A, "Q123456789", "unknown_entity"), (A, "Q910001", "not_a_person"),
                                      (A, A, "invalid_request")])
def test_failures_are_reported_not_fabricated(provider, s, t, code):
    state = run(provider, s, t)
    assert state.status == "failed"
    last = state.events.events[-1]
    assert last.type == "investigation.failed" and last.payload["error"]["code"] == code
    assert not state.graph.edges


def test_position_group_only_when_enabled(provider):
    pol = policy()
    off = run(provider, G, F)
    assert all(d["predicate"] != "P39" for e in off.events.events for d in e.payload.get("added_edges", []))
    on = run(provider, G, F, groups=pol.default_groups | {"position"})
    assert any(d["predicate"] == "P39" for e in on.events.events for d in e.payload.get("added_edges", []))


def test_fixture_gap_is_a_failed_tool_call_not_an_edge(provider):
    # Record only the endpoints: any further expansion must fail honestly.
    fx = FixtureProvider([build_case_fixture(provider, "gap", [A, B], ALL)])
    state = run(fx)
    errors = [e.payload for e in state.events.events if e.type == "tool.completed" and not e.payload["ok"]]
    assert errors and all(p["error"]["code"] == "fixture_gap" for p in errors)
    assert {r.qid for r in state.expansions if r.error} == {p["qid"] for p in errors}
    assert state.status in ("completed", "partial")


def test_fixture_reproduces_the_live_run(provider):
    live = run(provider)
    expanded = [r.qid for r in live.expansions]
    fx = FixtureProvider([build_case_fixture(provider, "case", expanded, ALL)])
    assert comparable(run(fx)) == comparable(live)


def test_trace_round_trip_reconstruct_and_replay(provider, tmp_path):
    live = run(provider)
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace_dict(live, "case", {"provider": provider.name})), encoding="utf-8")
    trace = load_trace(path)

    graph, cmp = reconstruct(trace["events"])
    assert [e.edge_id for e in graph.edges] == [e.edge_id for e in live.graph.edges]
    assert cmp == live.comparison
    assert compare_paths(graph, A, B, policy()) == live.comparison  # recomputation agrees with the record

    replay = InvestigationState(A, B, live.groups, live.budgets, run_mode="replay", snapshot=live.snapshot)
    replay_into(replay, trace)
    started = replay.events.events[0].payload
    assert started["run_mode"] == "replay" and started["replay_of"] == "case" and started["recorded_at"]
    assert [e.type for e in replay.events.events] == [e.type for e in live.events.events]
    assert replay.investigation_id != live.investigation_id
    assert replay.status == "completed" and replay.comparison == live.comparison


def test_trace_requires_finished_run_and_valid_ids(provider, tmp_path):
    pending = InvestigationState(A, B, frozenset({"family"}), SEARCH.budgets)
    with pytest.raises(ValueError):
        trace_dict(pending, "x", {})
    live = run(provider)
    data = trace_dict(live, "case", {})
    data["events"] = data["events"][1:]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_trace(bad)


def test_live_state_cannot_be_used_for_replay(provider):
    live = run(provider)
    with pytest.raises(ValueError):
        replay_into(live, {"events": live.events.events})


def test_budget_overrides_can_only_lower():
    b = SEARCH.budgets
    assert b.lowered({"max_provider_requests": 10_000}).max_provider_requests == b.max_provider_requests
    assert b.lowered({"max_provider_requests": 3}).max_provider_requests == 3
    with pytest.raises(ValueError):
        b.lowered({"bogus": 1})
