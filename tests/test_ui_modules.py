"""Workbench JS modules (DOM-free parts) run in Node against real backend output.

- turtle-parser.js (the module the workbench uses) reads backend projections with the backend's spans;
- mapping.js maps editor ranges <-> graph ids exactly, and refuses stale text;
- investigation.js replays a recorded trace into the same graph and paths as the backend.
Skipped when Node.js is not installed.
"""

import json
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
