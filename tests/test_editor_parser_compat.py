"""The browser editor's own Turtle parser must read backend projections with identical spans.

Extracts ``parseTurtle`` (and its constants) from graph-view/live-editor.html and runs it with
Node.js. Skipped when Node is not installed.
"""

import json
import re
import shutil
import subprocess

import pytest

from src.config import load_policy
from src.graph.model import Edge
from src.graph.turtle_projection import serialize_visible_turtle

from .test_turtle_projection import EDGES, NODES

NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const triples = parseTurtle(data.text);
console.log(JSON.stringify({
  count: triples.length,
  stmts: triples.map(t => [t.stmt[0], t.stmt[1]]),
}));
"""


def _parser_source(root) -> str:
    html = (root / "graph-view" / "live-editor.html").read_text(encoding="utf-8")
    start = html.index("const RDF_TYPE")
    end = html.index("function localName")
    return html[start:end]


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_editor_parser_reads_projection_with_same_spans(root, tmp_path):
    edges = EDGES + [Edge("Q900002", "P3373", "Q900003", "sandbox", origin="sandbox")]
    proj = serialize_visible_turtle(NODES, edges, load_policy(), snapshot="20261104")

    data = tmp_path / "projection.json"
    data.write_text(json.dumps({"text": proj.text}), encoding="utf-8")
    script = tmp_path / "harness.js"
    source = re.sub(r"^// ═+.*$", "", _parser_source(root), flags=re.M)
    script.write_text(source + HARNESS, encoding="utf-8")

    out = subprocess.run([NODE, str(script), str(data)], capture_output=True, text=True, encoding="utf-8", check=True)
    parsed = json.loads(out.stdout)

    assert parsed["count"] == proj.triple_count
    js_spans = {tuple(s) for s in parsed["stmts"]}
    expected = {(s.start, s.end) for s in proj.edge_spans.values()}
    expected |= {(s.start, s.end) for spans in proj.node_spans.values() for s in spans}
    assert js_spans == expected
