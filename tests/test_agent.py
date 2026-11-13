"""Agent orchestrator with mocked models (Milestone 4). No network, no API key."""

import json
from types import SimpleNamespace

import pytest

from src.agent.client import AssistantTurn, OpenRouterChatClient, ToolCall
from src.agent.investigator import backend_summary, clip_summary, run_agent, verify_summary
from src.agent.prompts import build_system_prompt
from src.agent.tools import TOOL_SPECS, tool_definitions
from src.config import load_search_config
from src.investigations.baseline import run_deterministic
from src.investigations.replay import reconstruct
from src.investigations.service import DemoCases, InvestigationService
from src.investigations.state import InvestigationState
from src.wikidata.offline import SqlitePeopleGraphProvider

from .graph_helpers import build_people_db, policy
from .test_investigation import A, B, ORGS, PEOPLE, TRIPLES

C, D, H = "Q900003", "Q900004", "Q900008"
SEARCH = load_search_config()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory, request):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("agent"), PEOPLE, ORGS, TRIPLES)


@pytest.fixture
def provider(db_path):
    p = SqlitePeopleGraphProvider(db_path)
    yield p
    p.close()


class FakeChat:
    """Calls ``policy(messages) -> AssistantTurn`` each turn and records what it saw."""

    model = "fake/scripted"

    def __init__(self, decide):
        self.decide = decide
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        assert {t["function"]["name"] for t in tools} == set(TOOL_SPECS)
        return self.decide(messages)


_ids = iter(range(10**6))


def call(name, text="", **args):
    return AssistantTurn(text, [ToolCall(f"call-{next(_ids)}", name, json.dumps(args))])


def last_tool_result(messages):
    for m in reversed(messages):
        if m["role"] == "tool":
            return json.loads(m["content"])
    return None


def run(provider, chat, budgets=None, groups=None, search=SEARCH):
    pol = policy()
    state = InvestigationState(A, B, frozenset(groups or pol.default_groups), search.budgets.lowered(budgets),
                               strategy="agent", snapshot=provider.snapshot)
    return run_agent(state, provider, pol, search, chat)


def adaptive(messages):
    """A tiny 'agent' whose next action depends on what the previous tool returned."""
    n_tools = sum(1 for m in messages if m["role"] == "tool")
    last = last_tool_result(messages)
    if n_tools == 0:
        return call("get_neighbors", "Start with Ada's family.", qid=A, relation_groups=["family"])
    if n_tools == 1:
        return call("get_neighbors", "Now Bea's family.", qid=B, relation_groups=["family"])
    if last.get("path_found") or (last.get("shortest") is not None and "suggestions" not in last):
        if "shortest" not in last:
            return call("compute_paths", "A path appeared; check it.")
        qids = last["strongest"]["qids"]
        return call("finish", "Done.", summary=f"Ada ({A}) reaches Bea ({B}) through family: {' → '.join(qids)}.")
    if "suggestions" in last:
        # Insufficient results so far: follow the backend's top suggestion.
        return call("get_neighbors", "Follow the best-ranked family member.", qid=last["suggestions"][0]["qid"],
                    relation_groups=["family"])
    return call("get_search_status", "No path yet; see what is worth expanding.")


def test_agent_adapts_after_insufficient_results_and_matches_backend(provider, db_path):
    chat = FakeChat(adaptive)
    state = run(provider, chat)
    assert state.status == "completed" and state.stop_reason == "agent_finished"
    expanded = [r.qid for r in state.expansions]
    assert expanded[:2] == [A, B] and len(expanded) == 3 and expanded[2] in (C, D)  # third choice came from status

    # Same verifiable strongest path as the deterministic backend on the same data.
    pol = policy()
    det = run_deterministic(InvestigationState(A, B, pol.default_groups, SEARCH.budgets), provider, pol, SEARCH)
    assert state.comparison.strongest.result.path_id == det.comparison.strongest.result.path_id
    assert [n.qid for n in state.comparison.strongest.result.nodes] == [A, C, D, B]

    final = state.events.events[-1]
    assert final.type == "investigation.completed" and final.payload["scope"] == "explored_graph"
    answer = final.payload["answer"]
    assert answer["summary_source"] == "agent" and answer["summary_verified"]
    assert state.tool_calls <= state.budgets.max_agent_tool_calls
    decisions = [e.payload["summary"] for e in state.events.events if e.type == "agent.decision"]
    assert decisions[0] == "Start with Ada's family." and len(decisions) == len(chat.seen)


def test_events_follow_contract_and_replay_reconstructs(provider):
    state = run(provider, FakeChat(adaptive))
    events = state.events.events
    assert [e.event_id for e in events] == list(range(1, len(events) + 1))
    assert events[0].payload["strategy"] == "agent" and events[0].payload["model"] == "fake/scripted"
    graph, cmp = reconstruct(events)
    assert [e.edge_id for e in graph.edges] == [e.edge_id for e in state.graph.edges]
    assert cmp == state.comparison
    started = [e.payload["step"] for e in events if e.type == "tool.started"]
    assert started == [e.payload["step"] for e in events if e.type == "tool.completed"]


def test_tool_calls_outside_allowlist_are_rejected_without_graph_changes(provider):
    script = iter([
        call("get_neighbors", qid=H, relation_groups=["family"]),            # not explored yet
        call("get_neighbors", qid=A, relation_groups=["position"]),          # group not enabled
        call("get_neighbors", qid=A, relation_groups=["astrology"]),         # unknown group
        call("get_neighbors", qid="Q123", relation_groups=["family"], surprise=1),  # extra field
        AssistantTurn("", [ToolCall("x1", "run_any_sparql", '{"query": "SELECT *"}')]),
        AssistantTurn("", [ToolCall("x2", "get_neighbors", "{not json")]),
        call("inspect_path", path_id="nope"),
        call("finish", summary="Nothing found."),
    ])
    patient = SEARCH.model_copy(update={"agent": SEARCH.agent.model_copy(update={"no_progress_limit": 10})})
    state = run(provider, FakeChat(lambda m: next(script)), search=patient)
    errors = [e.payload["error"]["code"] for e in state.events.events
              if e.type == "tool.completed" and not e.payload["ok"]]
    assert errors == ["not_allowed", "not_allowed", "invalid_arguments", "invalid_arguments", "unknown_tool",
                      "invalid_arguments", "unknown_path"]
    assert not state.graph.edges and state.provider_requests == 0
    assert state.status == "completed" and state.events.events[-1].payload["outcome"] == "no_path_found"


def test_limit_is_capped_by_budget(provider):
    script = iter([call("get_neighbors", qid=A, relation_groups=["family", "institutional"], limit=10_000),
                   call("finish", summary="ok")])
    state = run(provider, FakeChat(lambda m: next(script)), budgets={"max_edges_per_expansion": 1})
    started = next(e for e in state.events.events if e.type == "tool.started" and e.payload["tool"] == "get_neighbors")
    assert started.payload["arguments"]["max_edges"] == 1
    assert len(state.graph.edges) == 1


def test_repeated_full_expansion_is_refused(provider):
    script = iter([call("get_neighbors", qid=A, relation_groups=["family"]),
                   call("get_neighbors", qid=A, relation_groups=["family"]),
                   call("finish", summary="ok")])
    state = run(provider, FakeChat(lambda m: next(script)))
    assert state.provider_requests == 1
    assert any(e.type == "tool.completed" and not e.payload["ok"] and e.payload["error"]["code"] == "already_expanded"
               for e in state.events.events)


def test_tool_call_budget_ends_with_partial(provider):
    state = run(provider, FakeChat(lambda m: call("get_search_status")), budgets={"max_agent_tool_calls": 2})
    assert state.status == "partial" and state.stop_reason == "max_agent_tool_calls"
    assert state.tool_calls == 2


def test_no_progress_and_max_turns_stop(provider):
    stuck = run(provider, FakeChat(lambda m: call("get_neighbors", qid=H, relation_groups=["family"])))
    assert stuck.status == "partial" and stuck.stop_reason == "no_progress"
    looping = run(provider, FakeChat(lambda m: call("compute_paths")))
    assert looping.status == "partial" and looping.stop_reason in ("max_turns", "max_agent_tool_calls")


def test_unverified_summary_is_replaced_by_backend_summary(provider):
    def decide(messages):
        if not any(m["role"] == "tool" for m in messages):
            return call("get_neighbors", qid=A, relation_groups=["family", "institutional"])
        if sum(m["role"] == "tool" for m in messages) == 1:
            return call("get_neighbors", qid=B, relation_groups=["family", "institutional"])
        return call("finish", summary="They are linked via Douglas Adams (Q42), a close friend of both.")

    state = run(provider, FakeChat(decide))
    answer = state.events.events[-1].payload["answer"]
    assert answer["summary_source"] == "backend" and not answer["summary_verified"]
    assert answer["unverified_mentions"] == ["Q42"]
    assert "Q42" not in answer["summary"] and "Shortest" in answer["summary"]


def test_plain_text_reply_finishes_and_model_errors_are_honest(provider):
    done = run(provider, FakeChat(lambda m: AssistantTurn("I cannot find anything.", [])))
    assert done.status == "completed" and done.stop_reason == "agent_finished"
    assert done.events.events[-1].payload["outcome"] == "no_path_found"

    def boom(messages):
        raise TimeoutError("upstream timed out")

    failed = run(provider, FakeChat(boom))
    assert failed.status == "partial" and failed.stop_reason == "agent_error"
    last = failed.events.events[-1].payload
    assert last["error"]["code"] == "agent_error" and last["answer"]["summary_source"] == "backend"


def test_verify_summary_rules():
    assert verify_summary("", None, 100)["summary_source"] == "backend"
    no_path = verify_summary("Q1 and Q2 are cousins.", None, 100)
    assert no_path["summary_source"] == "backend" and no_path["unverified_mentions"] == ["Q1", "Q2"]
    assert "No connection" in backend_summary(None)
    assert len(verify_summary("x " * 500, None, 60)["summary"]) <= 60


def test_prompt_is_small_and_tools_are_typed():
    pol = policy()
    prompt = build_system_prompt(pol, SEARCH.budgets, pol.default_groups)
    assert len(prompt) < 4000 and "@prefix" not in prompt and "PREFIX" not in prompt
    assert "- family (Family): spouse P26" in prompt
    disabled_line = next(line for line in prompt.splitlines() if line.startswith("- Disabled"))
    assert "position" in disabled_line and "P39" not in prompt  # disabled groups: names only
    everything = build_system_prompt(pol, SEARCH.budgets, frozenset(pol.groups))
    assert len(everything) < 4000 and "P39" in everything and "- Disabled" not in everything
    defs = {d["function"]["name"]: d["function"]["parameters"] for d in tool_definitions()}
    assert set(defs) == set(TOOL_SPECS)
    assert defs["get_neighbors"]["required"] == ["qid", "relation_groups"]
    assert defs["get_neighbors"]["additionalProperties"] is False


def test_openrouter_client_parses_tool_calls():
    class FakeOpenAI:
        def with_options(self, **kw):
            self.options = kw
            return self

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kw):
            self.kwargs = kw
            fn = SimpleNamespace(name="get_search_status", arguments="")
            msg = SimpleNamespace(content="Checking.", tool_calls=[SimpleNamespace(id="t1", function=fn)])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake = FakeOpenAI()
    turn = OpenRouterChatClient(fake, "m").complete([{"role": "user", "content": "hi"}], tool_definitions())
    assert turn.content == "Checking." and turn.tool_calls == [ToolCall("t1", "get_search_status", "{}")]
    assert fake.options["timeout"] > 0 and fake.kwargs["tool_choice"] == "auto"
    assert turn.as_message()["tool_calls"][0]["function"]["name"] == "get_search_status"


def test_service_agent_mode_and_health(provider):
    svc = InvestigationService(provider, demo_cases=DemoCases(version=1),
                               chat_factory=lambda: FakeChat(adaptive), agent_available=lambda: True)
    state = svc.start({"source_qid": A, "target_qid": B, "mode": "agent"}, background=False)
    assert state.strategy == "agent" and state.status == "completed"
    assert svc.health()["agent"]["available"] is True
    off = InvestigationService(provider, demo_cases=DemoCases(version=1), chat_factory=lambda: None,
                               agent_available=lambda: False)
    health = off.health()["agent"]
    assert health["available"] is False and "deterministic" in health["fallback"]


def test_llm_provider_resolution():
    from src.agent.client import resolve_llm_settings

    assert resolve_llm_settings({}) is None
    ds = resolve_llm_settings({"DEEPSEEK_API_KEY": "sk-abc"})
    assert (ds.provider, ds.base_url, ds.model) == ("deepseek", "https://api.deepseek.com", "deepseek-flash")
    assert "sk-abc" not in repr(ds)
    both = {"OPENROUTER_API_KEY": "sk-or-x", "DEEPSEEK_API_KEY": "sk-y", "DEEPSEEK_MODEL": "deepseek-reasoner"}
    assert resolve_llm_settings(both).provider == "openrouter"
    picked = resolve_llm_settings({**both, "LLM_PROVIDER": "DeepSeek"})
    assert (picked.provider, picked.model) == ("deepseek", "deepseek-reasoner")
    assert resolve_llm_settings({"LLM_PROVIDER": "deepseek", "OPENROUTER_API_KEY": "sk-or-x"}) is None
    with pytest.raises(ValueError):
        resolve_llm_settings({"LLM_PROVIDER": "nope"})


def test_improvement_leads_point_to_the_stronger_route(provider):
    # After expanding both endpoints, A-U-B (2 hops, institutional) exists; A's spouse C and B's
    # spouse D are unexpanded and could still give a stronger route.
    from src.agent.tools import Toolbox

    pol = policy()
    state = InvestigationState(A, B, pol.default_groups, SEARCH.budgets, strategy="agent", snapshot=provider.snapshot)
    from src.investigations.steps import begin
    assert begin(state, provider, pol, SEARCH)
    tb = Toolbox(state, provider, pol, SEARCH)
    for q in (A, B):
        res = tb.call("get_neighbors", json.dumps({"qid": q, "relation_groups": ["family", "institutional"]}), 1, "")
    assert res["path_found"] and res["stronger_path_possible"]
    assert {lead["qid"] for lead in res["improvement_leads"]} >= {C, D}
    tb.call("get_neighbors", json.dumps({"qid": C, "relation_groups": ["family", "institutional"]}), 2, "")
    final = tb.call("compute_paths", "{}", 3, "")
    assert final["strongest"]["qids"] == [A, C, D, B]
    assert final["stronger_path_possible"] is False and final["improvement_leads"] == []


def test_budget_stop_gets_a_verified_wrap_up_summary(provider):
    def decide(messages):
        if "Do not call any tools" in messages[-1]["content"]:
            return AssistantTurn(f"Search cut short; Ada ({A}) and Bea ({B}) share Small College.", [])
        n = sum(m["role"] == "tool" for m in messages)
        return call("get_neighbors", qid=[A, B][n % 2], relation_groups=["institutional"]) if n < 2 \
            else call("get_search_status")

    state = run(provider, FakeChat(decide), budgets={"max_agent_tool_calls": 3})
    assert state.status == "partial" and state.stop_reason == "max_agent_tool_calls"
    answer = state.events.events[-1].payload["answer"]
    assert answer["summary_source"] == "agent" and "cut short" in answer["summary"]
    assert any(e.type == "agent.decision" and e.payload["turn"] == "wrap-up" for e in state.events.events)


def test_clip_summary_ends_at_a_sentence_or_word_never_mid_word():
    text = "First sentence is here. Second one is a bit longer. Third mentions Ticiane Pinheiro and more."
    assert clip_summary(text, 200) == text
    assert clip_summary(text, 60) == "First sentence is here. Second one is a bit longer."
    assert clip_summary(text, len("First sentence is here.")) == "First sentence is here."
    one = "A single long sentence without any full stop that keeps on going and going"
    out = clip_summary(one, 30)
    assert out.endswith("…") and len(out) <= 30 and one.startswith(out[:-1]) and not out[:-1].endswith(" ")
    assert out[:-1] in ("A single long sentence", "A single long sentence without")
