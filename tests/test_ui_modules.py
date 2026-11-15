"""Workbench JS modules (DOM-free parts) run in Node against real backend output.

- turtle-parser.js (the module the workbench uses) reads backend projections with the backend's spans;
- mapping.js maps editor ranges <-> graph ids exactly, and refuses stale text;
- investigation.js replays a recorded trace into the same graph and paths as the backend.
Skipped when Node.js is not installed.
"""

import json
import re
import shutil
import subprocess

import pytest

from src.config import load_policy, load_search_config
from src.graph.model import Edge
from src.graph.turtle_projection import serialize_visible_turtle
from src.investigations.baseline import run_deterministic
from src.investigations.replay import trace_dict
from src.investigations.state import InvestigationState
from src.wikidata.offline import SqlitePeopleGraphProvider

from .graph_helpers import build_people_db
from .test_investigation import A, B, ORGS, PEOPLE, TRIPLES
from .test_turtle_projection import EDGES, NODES

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def run_node(root, tmp_path, script: str, data: dict) -> dict:
    js = root / "src" / "ui" / "js"
    (tmp_path / "data.json").write_text(json.dumps(data), encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        "import fs from 'node:fs';\n"
        f"const mod = (n) => import(new URL('file:///{js.as_posix()}/' + n));\n"
        "const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));\n"
        + script, encoding="utf-8")
    out = subprocess.run([NODE, str(harness), str(tmp_path / "data.json")], capture_output=True, text=True,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def edge_payloads(edges):
    return [{"id": e.edge_id, "subject": e.subject, "predicate": e.pid, "object": e.object, "origin": e.origin}
            for e in edges]


def test_mapping_matches_backend_spans_and_guards_stale_text(root, tmp_path):
    edges = EDGES + [Edge("Q900002", "P3373", "Q900003", "sandbox", origin="sandbox")]
    proj = serialize_visible_turtle(NODES, edges, load_policy(), snapshot="20261104")
    script = """
const { buildIndex, selectionToGraph, graphToSpans } = await mod('mapping.js');
const idx = buildIndex(data.text, data.edges);
const edgeSpans = Object.fromEntries([...idx.edgeSpans].map(([k, v]) => [k, v]));
const labelSpans = Object.fromEntries([...idx.nodeLabelSpans].map(([k, v]) => [k, v]));
const sel = {};
for (const e of data.edges) {
  const [a, b] = idx.edgeSpans.get(e.id)[0];
  const hit = selectionToGraph(idx, [[a + 1, a + 2]], data.text);
  sel[e.id] = { edges: [...hit.edges], nodes: [...hit.nodes].sort() };
}
const stale = selectionToGraph(idx, [[0, 5]], data.text + ' ');
const back = graphToSpans(idx, { edges: [data.edges[0].id] }, data.text);
console.log(JSON.stringify({ triples: idx.triples, edgeSpans, labelSpans, sel, stale: stale.stale,
                             staleSpans: graphToSpans(idx, { edges: [data.edges[0].id] }, 'x').stale, back: back.spans }));
"""
    out = run_node(root, tmp_path, script, {"text": proj.text, "edges": edge_payloads(edges)})
    assert out["triples"] == proj.triple_count
    assert {k: v[0] for k, v in out["edgeSpans"].items()} == {k: [s.start, s.end] for k, s in proj.edge_spans.items()}
    assert {k: v for k, v in out["labelSpans"].items()} == {k: [[s.start, s.end] for s in v] for k, v in proj.node_spans.items()}
    for e in edges:
        assert out["sel"][e.edge_id]["edges"] == [e.edge_id]
        assert out["sel"][e.edge_id]["nodes"] == sorted({e.subject, e.object})
    assert out["stale"] is True and out["staleSpans"] is True
    first = proj.edge_spans[edges[0].edge_id]
    assert out["back"] == [[first.start, first.end]]


def test_reducer_replays_a_recorded_trace_to_the_backend_state(root, tmp_path):
    db = build_people_db(root, tmp_path, PEOPLE, ORGS, TRIPLES)
    provider = SqlitePeopleGraphProvider(db)
    pol, search = load_policy(), load_search_config()
    state = run_deterministic(InvestigationState(A, B, pol.default_groups, search.budgets, snapshot=provider.snapshot),
                              provider, pol, search)
    provider.close()
    trace = trace_dict(state, "case", {})
    script = """
const { InvestigationModel } = await mod('investigation.js');
const m = new InvestigationModel();
const changes = data.events.map((ev) => m.apply(ev).type);
const dup = m.apply(data.events[3]).type;
const tools = m.timeline.filter((r) => r.kind === 'tool');
console.log(JSON.stringify({
  status: m.status, nodes: [...m.nodes.keys()].sort(), edges: [...m.edges.keys()].sort(),
  shortest: m.comparison?.shortest?.result?.path_id, strongest: m.comparison?.strongest?.result?.path_id,
  expanded: [...m.expanded].sort(), dup, toolRows: tools.length,
  toolStatuses: [...new Set(tools.map((r) => r.status))],
  rowsWithEdges: tools.filter((r) => r.edgeIds.length).length,
  final: m.timeline.at(-1).kind, pathEdges: m.pathEdgeIds().strongest,
}));
"""
    out = run_node(root, tmp_path, script, trace)
    assert out["status"] == "completed" and out["final"] == "final" and out["dup"] == "duplicate"
    assert out["nodes"] == sorted(n.qid for n in state.graph.nodes)
    assert out["edges"] == sorted(e.edge_id for e in state.graph.edges)
    assert out["shortest"] == state.comparison.shortest.result.path_id
    assert out["strongest"] == state.comparison.strongest.result.path_id
    assert out["expanded"] == sorted({r.qid for r in state.expansions})
    assert out["toolRows"] == len(state.expansions) and out["toolStatuses"] == ["ok"]
    assert out["rowsWithEdges"] >= 1
    assert out["pathEdges"] == state.comparison.strongest.result.edge_ids


def test_workbench_parser_module_equals_legacy_editor_parser(root, tmp_path):
    """The module is an extraction of the editor's parser; both must produce identical statements."""
    proj = serialize_visible_turtle(NODES, EDGES, load_policy(), snapshot="20261104")
    html = (root / "graph-view" / "live-editor.html").read_text(encoding="utf-8")
    legacy = html[html.index("const RDF_TYPE"):html.index("function localName")]
    legacy = "\n".join(line for line in legacy.splitlines() if not line.startswith("// ═"))
    script = ("const { parseTurtle } = await mod('turtle-parser.js');\n"
              "const legacy = new Function(data.legacy + '\\nreturn parseTurtle;')();\n"
              "const a = parseTurtle(data.text).map((t) => [t.stmt, t.subj.value, t.pred.value, t.obj.value]);\n"
              "const b = legacy(data.text).map((t) => [t.stmt, t.subj.value, t.pred.value, t.obj.value]);\n"
              "console.log(JSON.stringify({ same: JSON.stringify(a) === JSON.stringify(b), n: a.length }));\n")
    out = run_node(root, tmp_path, script, {"text": proj.text, "legacy": legacy})
    assert out == {"same": True, "n": proj.triple_count}


def test_step_book_groups_recorded_traces_into_presenter_steps(root, tmp_path):
    """Step-through (plan §2): every event lands in exactly one step, in order; agent turns become a
    decision step plus one step per tool call, deterministic steps are expansions, and the terminal
    event is its own last step."""
    traces = {name: json.loads((root / "data" / "fixtures" / "event_traces" / f"{name}.json").read_text(encoding="utf-8"))["events"]
              for name in ("lula-snoop-agent", "diana-churchill")}
    script = """
const { StepBook, describeStep, toolTarget, toolPurpose, orderBadge } = await mod('steps.js');
const { InvestigationModel } = await mod('investigation.js');
const out = {};
for (const [name, events] of Object.entries(data)) {
  const book = new StepBook();
  const progress = [];
  for (const ev of events) { book.push(ev); progress.push(book.lastComplete); }
  book.push(events[3]);  // a duplicate (SSE reconnect) is ignored
  const desc = book.steps.map(describeStep);
  // Rewinding = folding the steps' events up to k; the last step must equal the full fold.
  const full = new InvestigationModel(); events.forEach((e) => full.apply(e));
  const upTo = new InvestigationModel(); book.steps.forEach((s) => s.events.forEach((e) => upTo.apply(e)));
  out[name] = {
    kinds: book.steps.map((s) => s.kind),
    firstTypes: book.steps.map((s) => s.events[0].type),
    ids: book.steps.flatMap((s) => s.events.map((e) => e.event_id)),
    done: book.done, count: book.count, lastComplete: book.lastComplete, progress,
    addedEdges: desc.reduce((n, d) => n + d.tools.reduce((m, t) => m + t.addedEdges, 0), 0),
    edgeIds: desc.reduce((n, d) => n + d.edgeIds.length, 0),
    decisions: desc.filter((d) => d.decision).length,
    rules: desc.filter((d) => d.rule).length,
    toolsPerCall: book.steps.filter((s) => s.kind === "call").map((s) => s.events.filter((e) => e.type === "tool.started").length),
    callNos: book.steps.map((s, i) => s.kind === "call" ? [s.callNo, book.callCount(i), book.steps[s.parent].kind] : null).filter(Boolean),
    plans: book.steps.map((s, i) => s.kind === "decision" ? book.plan(i).length : null).filter((n) => n !== null),
    pathEvents: desc.map((d) => d.pathEvent).filter(Boolean),
    purposes: desc.flatMap((d) => d.tools.map((t) => toolPurpose(t))).every((x) => x.length > 0),
    badges: [orderBadge(1), orderBadge(20), orderBadge(21), orderBadge(50), orderBadge(51)],
    result: desc[desc.length - 1].result,
    sameFold: full.nodes.size === upTo.nodes.size && full.edges.size === upTo.edges.size,
    edgesTotal: full.edges.size,
    target: toolTarget(desc.find((d) => d.tools.some((t) => t.qid)).tools.find((t) => t.qid)),
  };
}
console.log(JSON.stringify(out));
"""
    out = run_node(root, tmp_path, script, traces)
    for name, events in traces.items():
        r = out[name]
        assert r["ids"] == [e["event_id"] for e in events]          # nothing lost, duplicated or reordered
        assert r["kinds"][0] == "setup" and r["kinds"][-1] == "result" and r["done"]
        assert r["firstTypes"][-1] == events[-1]["type"] and r["lastComplete"] == r["count"] - 1
        assert r["sameFold"] and r["addedEdges"] == r["edgeIds"] == r["edgesTotal"] - 0
        assert r["result"]["outcome"] == events[-1]["payload"]["outcome"]
        # While streaming, a step only becomes showable once the next one has started.
        assert r["progress"] == sorted(r["progress"]) and r["progress"][0] == -1
        assert "side" in r["target"]
        assert r["purposes"]                                         # every tool has a plain description
        assert r["badges"] == ["①", "⑳", "㉑", "㊿", "(51)"]
        assert r["pathEvents"][0] == "first" and "first" not in r["pathEvents"][1:]
    agent = out["lula-snoop-agent"]
    events = traces["lula-snoop-agent"]
    n_decisions = sum(e["type"] == "agent.decision" for e in events)
    n_calls = sum(e["type"] == "tool.started" for e in events)
    assert set(agent["kinds"][1:-1]) == {"decision", "call"} and agent["decisions"] == n_decisions
    assert agent["kinds"].count("call") == n_calls and set(agent["toolsPerCall"]) == {1}  # one call per step
    assert all(kind == "decision" and 1 <= no <= count for no, count, kind in agent["callNos"])
    assert sum(agent["plans"]) == n_calls
    det = out["diana-churchill"]
    n_expansions = sum(e["type"] == "tool.started" for e in traces["diana-churchill"])
    assert det["kinds"][1:-1] == ["expansion"] * n_expansions and det["rules"] == n_expansions


def test_step_book_folds_the_fruitless_search_after_the_last_path_change(root, tmp_path):
    """Once a run ends, the steps after the strongest path last changed become one "tail" step (Lula–Snoop:
    after the path appears the agent follows a dozen more leads that change nothing). The fold loses no event,
    keeps every plan intact, never touches a step already on screen, and unfolds back exactly."""
    names = [p.stem for p in sorted((root / "data" / "fixtures" / "event_traces").glob("*.json"))]
    traces = {n: json.loads((root / "data" / "fixtures" / "event_traces" / f"{n}.json").read_text(encoding="utf-8"))["events"]
              for n in names}
    script = """
const { StepBook, describeTail, MIN_FOLD } = await mod('steps.js');
const fill = (events) => { const b = new StepBook(); events.forEach((e) => b.push(e)); return b; };
const sig = (b) => b.steps.map((s) => s.kind + ':' + s.events.map((e) => e.event_id).join(','));
const out = {};
for (const [name, events] of Object.entries(data)) {
  const book = fill(events);
  const before = sig(book), plansBefore = book.steps.map((s, i) => s.kind === 'decision' ? JSON.stringify(book.plan(i)) : null);
  const pathSteps = book.steps.map((s, i) => s.pathEvent ? i : -1).filter((i) => i >= 0);
  const folded = book.foldTail(0);
  const tailAt = book.steps.findIndex((s) => s.kind === 'tail');
  const tail = tailAt >= 0 ? book.steps[tailAt] : null;
  const r = {
    folded, before: before.length, after: book.count, tailAt, lastPath: pathSteps.at(-1) ?? null,
    pathStep: tail?.pathStep ?? null, kindsAfterTail: tail ? book.steps.slice(tailAt + 1).map((s) => s.kind) : null,
    ids: book.steps.flatMap((s) => s.events.map((e) => e.event_id)),
    plansKept: book.steps.slice(0, tailAt < 0 ? undefined : tailAt).every((s, i) => s.kind !== 'decision' || JSON.stringify(book.plan(i)) === plansBefore[i]),
    tail: tail ? describeTail(tail) : null,
    again: book.foldTail(0),
  };
  const at = book.unfoldTail();
  r.unfoldAt = at; r.restored = JSON.stringify(sig(book)) === JSON.stringify(before); r.refold = book.foldTail(0);
  // A presenter already past the path (live run) keeps the steps they have seen.
  const live = fill(events);
  r.liveFold = live.foldTail(before.length - 2);
  out[name] = r;
}
out.MIN_FOLD = MIN_FOLD;
console.log(JSON.stringify(out));
"""
    out = run_node(root, tmp_path, script, traces)
    for name, events in traces.items():
        r = out[name]
        assert r["ids"] == [e["event_id"] for e in events]  # nothing lost or reordered
        assert r["again"] is False and r["refold"] is False and r["liveFold"] is False
        if r["folded"]:
            assert r["tailAt"] == r["lastPath"] + 1 == r["pathStep"] + 1 and r["kindsAfterTail"] == ["result"]
            assert r["before"] - r["after"] == r["tail"]["steps"] - 1 and r["tail"]["steps"] >= out["MIN_FOLD"]
            assert r["plansKept"] and r["unfoldAt"] == r["tailAt"] and r["restored"]
        else:
            assert r["lastPath"] is None or r["before"] - 2 - r["lastPath"] < out["MIN_FOLD"]
    lula = out["lula-snoop-agent"]
    assert lula["folded"] and lula["tail"]["steps"] >= 15 and len(lula["tail"]["expansions"]) >= 10
    lula_decisions = [e["payload"].get("summary") for e in traces["lula-snoop-agent"] if e["type"] == "agent.decision"]
    assert lula["tail"]["reasonVerbatim"] in lula_decisions           # the agent's own words, kept verbatim
    assert lula["tail"]["reason"] and not re.search(r"\d\.\d", lula["tail"]["reason"])  # shown without costs
    assert lula["tail"]["addedNodes"] > 0 and lula["tail"]["otherCalls"].get("compute_paths", 0) >= 1
    assert out["diana-churchill"]["folded"]                          # deterministic: expansions after the first path


def test_path_moments_are_explained_from_recorded_facts(root, tmp_path):
    """The path step card explains the moment in words, from the recording only: where the two sides
    met (a hop whose ends were first reached from different sides, joined by this step's statement),
    link strengths as words that follow the policy, and why a replacing path is stronger."""
    names = [p.stem for p in sorted((root / "data" / "fixtures" / "event_traces").glob("*.json"))]
    traces = {n: json.loads((root / "data" / "fixtures" / "event_traces" / f"{n}.json").read_text(encoding="utf-8"))["events"]
              for n in names}
    script = """
const S = await mod('steps.js');
const out = { words: [0.95, 0.85, 0.8, 0.6, 0.5, 0.35, 0.3, 0.1, null].map(S.strengthWord), cases: {} };
for (const [name, events] of Object.entries(data)) {
  const book = new S.StepBook(); events.forEach((e) => book.push(e));
  const moments = [];
  book.steps.forEach((s, i) => {
    const d = S.describeStep(s);
    if (!d.pathEvent) return;
    const now = S.pathChain(d.pathResult);
    const m = S.meetingPoint(now, d.sides, d.expandedQids[0], d.edgeIds);
    moments.push({ kind: d.pathEvent, hops: now.hops, words: now.rels.map((r) => [r.strength, r.word]),
      summary: S.strengthSummary(now), meet: m && { meet: m.meet.qid, already: m.already, via: m.viaThisStep,
        sides: [d.sides.get(m.from.qid), d.sides.get(m.to.qid)], mine: d.sides.get(d.expandedQids[0]) },
      why: d.prevPath ? S.whyStronger(S.pathChain(d.prevPath), now) : null,
      prevCost: d.prevPath?.total_cost ?? null, cost: d.pathResult.total_cost });
  });
  const tailBook = new S.StepBook(); events.forEach((e) => tailBook.push(e)); tailBook.foldTail(0);
  const tail = tailBook.steps.find((s) => s.kind === 'tail');
  out.cases[name] = { moments,
    tailLeads: tail ? S.describeTail(tail, tailBook.latestLeads(tailBook.steps.indexOf(tail) - 1)).fromLeads : 'none',
    hasLeads: events.some((e) => e.type === 'tool.leads'), agent: events[0].payload.strategy === 'agent',
    guidance: book.steps.map((s, i) => book.unrecordedGuidance(i)).filter(Boolean),
    firstPathStep: book.steps.findIndex((s) => s.pathEvent),
    guidanceSteps: book.steps.map((s, i) => book.unrecordedGuidance(i) === 'leads' ? i : -1).filter((i) => i >= 0) };
}
// A synthetic lead list: leadsFor returns it for the next decision, and the graph growing makes it stale.
const ev = (id, type, payload) => ({ event_id: id, type, payload });
const b = new S.StepBook();
[ev(1, 'investigation.started', { strategy: 'agent' }), ev(2, 'agent.decision', { summary: 'go', tool_calls: ['get_neighbors'] }),
 ev(3, 'tool.started', { tool: 'get_neighbors', step: 1, qid: 'Q1' }),
 ev(4, 'tool.leads', { tool: 'get_neighbors', step: 1, stronger_path_possible: true, improvement_leads: [{ rank: 1, qid: 'Q7' }], more_leads: 0 }),
 ev(5, 'agent.decision', { summary: 'lead', tool_calls: ['get_neighbors'] }),
 ev(6, 'tool.started', { tool: 'get_neighbors', step: 2, qid: 'Q7' }),
 ev(7, 'graph.expanded', { step: 2, expanded_qid: 'Q7', side: 'source', added_nodes: [{ id: 'Q8' }], added_edges: [{ id: 'e' }] }),
 ev(8, 'agent.decision', { summary: 'again', tool_calls: [] })].forEach((e) => b.push(e));
out.synthetic = { fresh: b.leadsFor(3), viaCall: b.leadsFor(4), stale: b.leadsFor(5), latest: b.latestLeads(5) };
console.log(JSON.stringify(out));
"""
    out = run_node(root, tmp_path, script, traces)
    assert out["words"] == ["strong", "strong", "strong", "medium", "medium", "weak", "weak", "weak", None]
    for name, r in out["cases"].items():
        assert r["moments"] and r["moments"][0]["kind"] == "first", name
        for m in r["moments"]:
            assert all(w in ("strong", "medium", "weak") for _, w in m["words"]) and m["summary"]
            if m["kind"] == "first" and m["meet"]:
                # the joining hop's ends really were first reached from different sides; the meeting
                # node is the end the expanded node's side did NOT reach first
                assert set(m["meet"]["sides"]) == {"source", "target"} and m["meet"]["already"] != m["meet"]["mine"]
            if m["kind"] == "improved":
                assert m["why"] and m["cost"] < m["prevCost"]
        if r["agent"] and not r["hasLeads"]:
            # every decision after the first path discloses the unrecorded lead list
            assert r["guidanceSteps"] and min(r["guidanceSteps"]) > r["firstPathStep"]
        if not r["agent"]:
            assert r["guidance"] == []
        assert r["tailLeads"] in (None, "none") or isinstance(r["tailLeads"], int)
    assert any(m["meet"] and m["meet"]["via"] for r in out["cases"].values() for m in r["moments"])
    syn = out["synthetic"]
    assert syn["fresh"]["leads"][0]["qid"] == "Q7" and syn["viaCall"] == syn["fresh"] and syn["stale"] is None
    assert syn["latest"][0]["qid"] == "Q7"


def test_step_book_exposes_the_recorded_suggestions_the_agent_saw(root, tmp_path):
    """get_search_status records its ranked suggestions; the step card and the ranking effect read them
    from the event (never recomputed). Recordings made before that return no ranking."""
    from .test_agent import FakeChat, adaptive, run

    provider = SqlitePeopleGraphProvider(build_people_db(root, tmp_path, PEOPLE, ORGS, TRIPLES))
    state = run(provider, FakeChat(adaptive))
    provider.close()
    events = [{"event_id": e.event_id, "investigation_id": "x", "type": e.type, "timestamp": e.timestamp,
               "payload": e.payload} for e in state.events.events]
    recorded = [e["payload"]["suggestions"] for e in events
                if e["type"] == "tool.completed" and e["payload"]["tool"] == "get_search_status"]
    # An older recording: the same trace without the ranking fields (recordings before 2026-11-14 had none).
    old = json.loads((root / "data" / "fixtures" / "event_traces" / "lula-snoop-agent.json").read_text(encoding="utf-8"))["events"]
    for e in old:
        if e["type"] == "tool.completed":
            e["payload"].pop("suggestions", None); e["payload"].pop("frontier_size", None)
    script = """
const { StepBook, describeStep } = await mod('steps.js');
const book = (events) => { const b = new StepBook(); events.forEach((e) => b.push(e)); return b; };
const b = book(data.events);
const statusAt = b.steps.findIndex((s) => describeStep(s).tools.some((t) => t.tool === 'get_search_status'));
const t = describeStep(b.steps[statusAt]).tools[0];
const old = book(data.old);
console.log(JSON.stringify({
  cardList: t.suggestions, size: t.frontierSize,
  before: b.lastSuggestions(statusAt - 1), at: b.lastSuggestions(statusAt), after: b.lastSuggestions(b.count - 1),
  old: old.steps.map((_, i) => old.lastSuggestions(i) || old.rankingFor(i)).filter(Boolean).length,
  // the agent's next decision after the status turn is judged against that list; its own turn is not
  nextDecision: b.rankingFor(b.steps.findIndex((s, i) => i > statusAt && s.kind === 'decision')),
  ownTurn: b.rankingFor(b.steps[statusAt].parent),
}));
"""
    out = run_node(root, tmp_path, script, {"events": events, "old": old})
    assert recorded and out["cardList"] == recorded[0] and out["size"] >= len(recorded[0])
    assert out["before"] is None and out["at"]["suggestions"] == recorded[0]
    assert out["after"]["suggestions"] == recorded[-1]
    assert out["old"] == 0  # no ranking is ever invented for older recordings
    assert out["nextDecision"]["suggestions"] == recorded[0] and out["ownTurn"] is None


def test_result_step_shows_the_connection_as_chains(root, tmp_path):
    """The final step card shows the strongest path (the only one the demo shows) as a chain, in
    hop order from source to target, with direction-aware relation labels, and an honest headline."""
    traces = {name: json.loads((root / "data" / "fixtures" / "event_traces" / f"{name}.json").read_text(encoding="utf-8"))["events"]
              for name in ("lula-snoop-agent", "diana-churchill", "coelho-senna-agent")}
    script = """
const { pathChains, resultHeadline } = await mod('steps.js');
const out = {};
for (const [name, events] of Object.entries(data)) {
  const last = events[events.length - 1];
  const r = { type: last.type, outcome: last.payload.outcome, stop: last.payload.stop_reason };
  out[name] = { chains: pathChains(last.payload.comparison), head: resultHeadline(r), comparison: last.payload.comparison };
}
out.none = { chains: pathChains(null), head: resultHeadline({ type: 'investigation.partial', outcome: 'budget_exhausted', stop: 'max_explored_nodes' }),
             failed: resultHeadline({ type: 'investigation.failed', error: { code: 'agent_error', message: 'x' } }) };
console.log(JSON.stringify(out));
"""
    out = run_node(root, tmp_path, script, traces)
    for name, events in traces.items():
        r, c = out[name], out[name]["comparison"]
        assert r["head"]["head"] == "Connection found" and "explored subgraph only" in r["head"]["scope"]
        if events[-1]["type"] == "investigation.partial":
            assert "stopped early" in r["head"]["scope"]
        expected = [("strongest", c["strongest"]["result"])]
        assert [ch["mode"] for ch in r["chains"]] == [m for m, _ in expected]
        for ch, (_, res) in zip(r["chains"], expected):
            assert ch["hops"] == res["hop_count"] == len(ch["rels"]) == len(ch["nodes"]) - 1
            assert [n["qid"] for n in ch["nodes"]] == [res["hops"][0]["from_qid"]] + [h["to_qid"] for h in res["hops"]]
            assert ch["nodes"][0]["role"] == "source" and ch["nodes"][-1]["role"] == "target"
            assert [x["label"] for x in ch["rels"]] == [h["label"] for h in res["hops"]]
            assert all(n["label"] for n in ch["nodes"])
    c = out["coelho-senna-agent"]["comparison"]  # recorded with strongest != shortest: only the strongest is shown
    assert not c["same_path"] and len(out["coelho-senna-agent"]["chains"]) == 1
    assert out["coelho-senna-agent"]["chains"][0]["hops"] == c["strongest"]["result"]["hop_count"]
    assert out["none"]["chains"] == [] and out["none"]["head"]["head"] == "No connection found yet"
    assert out["none"]["failed"]["tone"] == "err" and "agent_error" in out["none"]["failed"]["scope"]


def test_browser_subscribes_to_every_server_event_type(root, tmp_path):
    """SSE delivers named events only to listeners of that exact type: a type the server emits but
    api.js does not subscribe to vanishes silently (tool.leads did, until this test)."""
    from typing import get_args

    from src.investigations.events import EventType

    out = run_node(root, tmp_path, "const { EVENT_TYPES } = await mod('investigation.js');\n"
                                   "console.log(JSON.stringify(EVENT_TYPES));\n", {})
    assert sorted(out) == sorted(get_args(EventType))


def test_agent_messages_are_shown_without_cost_figures(root, tmp_path):
    """Recorded agent messages quote costs the screen never explains ("cost 3.947", "best_possible_cost
    0.865", "(degree 58)"). Every recorded message, as shown, has no decimal number or field name and
    still reads as a sentence; the exact words stay in a tooltip."""
    msgs = []
    for f in sorted((root / "data" / "fixtures" / "event_traces").glob("*agent*.json")):
        for ev in json.loads(f.read_text(encoding="utf-8"))["events"]:
            p = ev["payload"]
            if ev["type"] == "agent.decision" and p.get("summary"):
                msgs.append(p["summary"])
            if isinstance(p.get("answer"), dict) and p["answer"].get("summary"):
                msgs.append(p["answer"]["summary"])
    samples = ["A path now exists (cost 3.947) but a stronger one is possible.",
               "Pelé (degree 58) is a strong hub-like connector.",
               "The best_possible_cost is stuck at 1.479 and rising — the strong family links are exhausted.",
               "Still 3.718. Let me expand Harry.",
               "The best possible cost has risen to 2.293, still below 3.0, so a stronger path may exist.",
               "Expand Ada Lovelace and Charles Babbage."]
    script = """
const { plainWords, verbatimTitle } = await mod('investigation.js');
console.log(JSON.stringify({ shown: data.msgs.map(plainWords), samples: data.samples.map(plainWords),
                             titles: data.samples.map(verbatimTitle) }));
"""
    out = run_node(root, tmp_path, script, {"msgs": msgs, "samples": samples})
    assert len(msgs) > 50
    for raw, shown in zip(msgs, out["shown"]):
        assert not re.search(r"\d\.\d|_cost|degree \d|\(\s*\)|\s[,.;]|  ", shown), (raw, shown)
        assert len(shown) > 0.6 * len(raw) - 40, (raw, shown)   # numbers go, the sentence stays
    assert out["samples"] == [
        "A path now exists but a stronger one is possible.",
        "Pelé is a strong hub-like connector.",
        "The best possible cost keeps rising — the strong family links are exhausted.",
        "No stronger path yet. Let me expand Harry.",
        "The best possible cost has risen, so a stronger path may exist.",
        "Expand Ada Lovelace and Charles Babbage."]
    assert "cost 3.947" in out["titles"][0] and out["titles"][-1] is None


def test_paths_through_shared_entities_say_it_is_an_association(root, tmp_path):
    """A chain through an organisation, film or place carries the honesty rule; a chain of people does not."""
    def chain(*kinds):
        return {"nodes": [{"label": f"N{i}", "kind": k} for i, k in enumerate(kinds)]}
    script = """
const { associationNote } = await mod('steps.js');
console.log(JSON.stringify(data.map(associationNote)));
"""
    out = run_node(root, tmp_path, script, [
        chain("person", "organization", "person", "person"),
        chain("person", "work", "person", "work", "person"),
        chain("person", "organization", "person", "work", "person"),
        chain("person", "person", "person"),
        chain("person", "person")])
    assert out == [
        "Through N1: a shared organisation is an association, not proof that they met.",
        "Through N1, N3: a shared film or record is an association, not proof that they met.",
        "Through N1, N3: a shared organisation, work or place is an association, not proof that they met.",
        None, None]
