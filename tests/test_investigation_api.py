"""Plan §7 routes over an injected offline service (synthetic people graph, no network)."""

import json
import time

import pytest
from rdflib import Graph

from src.config import load_policy, load_search_config
from src.investigations.baseline import run_deterministic
from src.investigations.replay import trace_dict
from src.investigations.service import DemoCase, DemoCases, InvestigationService
from src.investigations.state import InvestigationState
from src.wikidata.offline import SqlitePeopleGraphProvider

from .test_investigation import A, B, ORGS, PEOPLE, TRIPLES, build_people_db


@pytest.fixture(scope="module")
def db_path(tmp_path_factory, request):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("api"), PEOPLE, ORGS, TRIPLES)


@pytest.fixture
def service(db_path, tmp_path):
    provider = SqlitePeopleGraphProvider(db_path)
    pol, search = load_policy(), load_search_config()
    live = run_deterministic(InvestigationState(A, B, pol.default_groups, search.budgets, snapshot=provider.snapshot),
                             provider, pol, search)
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps(trace_dict(live, "ada-bea", {})), encoding="utf-8")
    cases = DemoCases(version=1, cases=[DemoCase(id="ada-bea", title="Ada ↔ Bea", source_qid=A, source_label="Ada",
                                                 target_qid=B, target_label="Bea", trace="trace.json")])
    svc = InvestigationService(provider, pol, search, demo_cases=cases, root=tmp_path,
                               chat_factory=lambda: None, agent_available=lambda: False)
    yield svc
    provider.close()


@pytest.fixture
def api(app, service):
    app.config["INVESTIGATIONS"] = service
    yield app.test_client()
    app.config.pop("INVESTIGATIONS", None)


def wait_done(api, inv_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = api.get(f"/api/investigations/{inv_id}").get_json()
        if body["status"] in ("completed", "partial", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("investigation did not finish")


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if "data" in lines:
            events.append((int(lines["id"]), lines["event"], json.loads(lines["data"])))
    return events


def test_health_and_demo_cases(api):
    health = api.get("/api/health").get_json()
    assert health["provider"] == {"name": "people-graph-sqlite", "snapshot": "20991231"}
    assert health["replay_cases"] == ["ada-bea"]
    assert health["agent"]["available"] is False
    cases = api.get("/api/demo-cases").get_json()
    assert cases["cases"][0]["source_qid"] == A


def test_people_search(api):
    body = api.get("/api/people/search?q=ad").get_json()
    assert [m["qid"] for m in body["matches"]] == [A]
    assert api.get("/api/people/search?q=").get_json()["matches"] == []
    assert api.get("/api/people/search?q=" + "x" * 300).status_code == 400


def test_deterministic_run_events_paths_graph_and_turtle(api):
    res = api.post("/api/investigations", json={"source_qid": A, "target_qid": B})
    assert res.status_code == 202
    inv = res.get_json()["investigation_id"]
    assert wait_done(api, inv)["status"] == "completed"

    sse = api.get(f"/api/investigations/{inv}/events")
    assert sse.mimetype == "text/event-stream"
    events = parse_sse(sse.get_data(as_text=True))
    assert [i for i, _, _ in events] == list(range(1, len(events) + 1))
    assert events[0][1] == "investigation.started" and events[-1][1] == "investigation.completed"
    assert all(d["investigation_id"] == inv and d["type"] == t for _, t, d in events)

    resumed = parse_sse(api.get(f"/api/investigations/{inv}/events", headers={"Last-Event-ID": "3"}).get_data(as_text=True))
    assert [i for i, _, _ in resumed] == list(range(4, len(events) + 1))

    paths = api.get(f"/api/investigations/{inv}/paths").get_json()
    assert paths["comparison"]["shortest"]["result"]["hop_count"] == 2
    assert paths["comparison"]["scope"] == "explored_graph"

    graph = api.get(f"/api/investigations/{inv}/graph").get_json()
    assert {n["id"] for n in graph["nodes"]} >= {A, B}
    assert all(e["label"] for e in graph["edges"])

    ttl = api.get(f"/api/investigations/{inv}/turtle").get_json()
    assert len(Graph().parse(data=ttl["text"], format="turtle")) == ttl["triple_count"]
    assert set(ttl["edge_spans"]) == {e["id"] for e in graph["edges"]}
    edge_id = graph["edges"][0]["id"]
    sel = api.get(f"/api/investigations/{inv}/turtle?scope=selected&edge_ids={edge_id}").get_json()
    assert list(sel["edge_spans"]) == [edge_id]
    raw = api.get(f"/api/investigations/{inv}/turtle?format=text")
    assert raw.content_type.startswith("text/turtle")


def test_replay_is_marked_as_replay(api):
    res = api.post("/api/investigations", json={"mode": "replay", "case_id": "ada-bea", "replay_delay_seconds": 0})
    assert res.status_code == 202
    inv = res.get_json()["investigation_id"]
    summary = wait_done(api, inv)
    assert summary["run_mode"] == "replay" and summary["status"] == "completed"
    events = parse_sse(api.get(f"/api/investigations/{inv}/events").get_data(as_text=True))
    assert events[0][2]["payload"]["run_mode"] == "replay"
    assert events[0][2]["payload"]["replay_of"] == "ada-bea"


def test_cancel_endpoint(api, service):
    state = service.start({"source_qid": A, "target_qid": B, "mode": "replay", "case_id": "ada-bea",
                           "replay_delay_seconds": 0.2})
    assert api.post(f"/api/investigations/{state.investigation_id}/cancel").status_code == 200
    body = wait_done(api, state.investigation_id)
    assert body["status"] in ("partial", "completed")  # completed only if it finished first


@pytest.mark.parametrize("payload,status,code", [
    ({"source_qid": "Q1"}, 400, "invalid_request"),
    ({"source_qid": "x", "target_qid": "Q2"}, 400, "invalid_request"),
    ({"source_qid": A, "target_qid": B, "groups": ["nonsense"]}, 400, "invalid_request"),
    ({"source_qid": A, "target_qid": B, "budgets": {"bogus": 1}}, 400, "invalid_request"),
    ({"source_qid": A, "target_qid": B, "budgets": {"max_agent_tool_calls": 10_000}}, 400, "invalid_request"),
    ({"source_qid": A, "target_qid": B, "budgets": {"max_provider_requests": 0}}, 400, "invalid_request"),
    ({"source_qid": A, "target_qid": B, "max_turns": 10_000}, 400, "invalid_request"),
    ({"source_qid": A, "target_qid": B, "mode": "telepathy"}, 400, "unsupported_mode"),
    ({"source_qid": A, "target_qid": B, "mode": "agent"}, 503, "agent_unavailable"),
    ({"mode": "replay", "case_id": "missing"}, 404, "unknown_case"),
    ({"source_qid": A, "target_qid": B, "surprise": 1}, 400, "invalid_request"),
])
def test_start_validation(api, payload, status, code):
    res = api.post("/api/investigations", json=payload)
    assert res.status_code == status and res.get_json()["error"]["code"] == code


def test_unknown_investigation_and_missing_provider(api, app):
    assert api.get("/api/investigations/nope").status_code == 404
    app.config["INVESTIGATIONS"] = InvestigationService(None, demo_cases=DemoCases(version=1),
                                                        chat_factory=lambda: None, agent_available=lambda: False)
    res = api.post("/api/investigations", json={"source_qid": A, "target_qid": B})
    assert res.status_code == 503 and res.get_json()["error"]["code"] == "provider_unavailable"
    assert api.get("/api/health").get_json()["provider"] is None


def test_legacy_routes_still_work(api):
    assert api.get("/").status_code == 200
    assert api.get("/api/ontology").status_code == 200


def test_replay_works_with_no_people_graph_and_no_api_key(app, service, tmp_path):
    """Worst case on workshop day: no people graph, no LLM key, no network. Replays must still run."""
    bare = InvestigationService(None, demo_cases=service.demo_cases, root=service.root,
                                chat_factory=lambda: None, agent_available=lambda: False)
    app.config["INVESTIGATIONS"] = bare
    c = app.test_client()
    health = c.get("/api/health").get_json()
    assert health["provider"] is None and health["agent"]["available"] is False
    assert health["replay_cases"] == ["ada-bea"]
    res = c.post("/api/investigations", json={"mode": "replay", "case_id": "ada-bea", "replay_delay_seconds": 0})
    assert res.status_code == 202
    inv = res.get_json()["investigation_id"]
    assert wait_done(c, inv)["status"] == "completed"
    paths = c.get(f"/api/investigations/{inv}/paths").get_json()
    assert paths["comparison"]["shortest"]["result"]["hop_count"] == 2
    ttl = c.get(f"/api/investigations/{inv}/turtle").get_json()
    assert ttl["triple_count"] > 0
    assert c.post("/api/investigations", json={"source_qid": A, "target_qid": B}).status_code == 503
    assert c.post("/api/investigations", json={"source_qid": A, "target_qid": B, "mode": "agent"}).status_code == 503
    assert c.get("/api/people/search?q=ad").status_code == 503


def test_sse_resumes_after_a_dropped_connection(api):
    """EventSource reconnects with Last-Event-ID; the server must continue exactly after it."""
    inv = api.post("/api/investigations", json={"source_qid": A, "target_qid": B}).get_json()["investigation_id"]
    wait_done(api, inv)
    full = parse_sse(api.get(f"/api/investigations/{inv}/events").get_data(as_text=True))
    for cut in (1, len(full) // 2, len(full) - 1):
        rest = parse_sse(api.get(f"/api/investigations/{inv}/events", headers={"Last-Event-ID": str(cut)}).get_data(as_text=True))
        assert [i for i, _, _ in rest] == list(range(cut + 1, len(full) + 1))
    assert parse_sse(api.get(f"/api/investigations/{inv}/events?after=junk").get_data(as_text=True))[0][0] == 1


def test_options_can_raise_budgets_up_to_the_limits_and_the_run_records_them(api, service):
    """The Options menu: a run may set budgets above the defaults (up to config limits); the run uses
    and records exactly those, and /api/health tells the UI the defaults and the ceilings."""
    health = api.get("/api/health").get_json()
    lim, defaults = health["limits"], health["budgets"]
    assert health["agent_max_turns"] == service.search.agent.max_turns
    assert all(lim[k] >= defaults[k] for k in defaults) and lim["max_agent_turns"] >= health["agent_max_turns"]
    raised = {"max_provider_requests": lim["max_provider_requests"], "max_agent_tool_calls": defaults["max_agent_tool_calls"] + 1}
    res = api.post("/api/investigations", json={"source_qid": A, "target_qid": B, "budgets": raised})
    assert res.status_code in (200, 201, 202), res.get_json()
    inv = res.get_json()["investigation_id"]
    wait_done(api, inv)
    data = next(d for _, typ, d in parse_sse(api.get(f"/api/investigations/{inv}/events").get_data(as_text=True))
                if typ == "investigation.started")
    budgets = data.get("payload", data)["budgets"]
    assert {k: budgets[k] for k in raised} == raised
