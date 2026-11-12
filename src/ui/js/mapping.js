// Graph <-> Turtle source mapping (no DOM; unit-tested with Node).
//
// The index is built by parsing the CURRENT editor text with the browser parser, then matching each
// statement to graph elements by (subject, predicate, object, origin). It never trusts offsets from
// an older text: callers rebuild it after every edit (the "stale-offset guard"), and lookups refuse
// to run when `index.text !== currentText`.

import { parseTurtle } from "./turtle-parser.js";

export const NS = {
  wd: "http://www.wikidata.org/entity/",
  wdt: "http://www.wikidata.org/prop/direct/",
  sbx: "https://example.org/six-degrees/sandbox/prop/",
  rdfsLabel: "http://www.w3.org/2000/01/rdf-schema#label",
};

const local = (iri, ns) => (typeof iri === "string" && iri.startsWith(ns) ? iri.slice(ns.length) : null);

export function edgeKey(subject, predicate, object, origin) {
  return `${subject}|${predicate}|${object}|${origin === "sandbox" ? "sandbox" : "wikidata"}`;
}

/** Parse `text` and map its statements to graph ids. Throws on unsupported syntax. */
export function buildIndex(text, edges) {
  const byKey = new Map();
  for (const e of edges) byKey.set(edgeKey(e.subject, e.predicate, e.object, e.origin), e.id);

  const triples = parseTurtle(text);
  const statements = new Map(); // "a:b" -> {span, edgeIds:Set, nodeIds:Set}
  const edgeSpans = new Map(), nodeLabelSpans = new Map(), nodeMentionSpans = new Map();
  const push = (map, id, span) => { if (!map.has(id)) map.set(id, []); map.get(id).push(span); };

  for (const t of triples) {
    const span = t.stmt;
    const key = `${span[0]}:${span[1]}`;
    if (!statements.has(key)) statements.set(key, { span, edgeIds: new Set(), nodeIds: new Set() });
    const st = statements.get(key);
    const s = local(t.subj.value, NS.wd);
    if (t.pred.value === NS.rdfsLabel && s) {
      st.nodeIds.add(s); push(nodeLabelSpans, s, span);
      continue;
    }
    const o = t.obj.type === "iri" ? local(t.obj.value, NS.wd) : null;
    let pid = local(t.pred.value, NS.wdt), origin = "wikidata";
    if (pid === null) { pid = local(t.pred.value, NS.sbx); origin = "sandbox"; }
    if (!s || !o || pid === null) continue;
    st.nodeIds.add(s); st.nodeIds.add(o);
    push(nodeMentionSpans, s, span); push(nodeMentionSpans, o, span);
    const id = byKey.get(edgeKey(s, pid, o, origin));
    if (id) { st.edgeIds.add(id); push(edgeSpans, id, span); }
  }
  return { text, triples: triples.length, statements: [...statements.values()], edgeSpans, nodeLabelSpans, nodeMentionSpans };
}

/** Editor selection ranges [[a,b], ...] -> graph ids whose statements overlap them. */
export function selectionToGraph(index, ranges, currentText) {
  const nodes = new Set(), edges = new Set();
  if (!index || index.text !== currentText) return { nodes, edges, stale: true };
  for (const st of index.statements) {
    if (!ranges.some(([a, b]) => st.span[0] < b && st.span[1] > a)) continue;
    st.edgeIds.forEach((id) => edges.add(id));
    st.nodeIds.forEach((id) => nodes.add(id));
  }
  return { nodes, edges, stale: false };
}

/** Graph selection -> source spans to mark. Nodes prefer their label statements. */
export function graphToSpans(index, { nodes = [], edges = [] }, currentText) {
  if (!index || index.text !== currentText) return { spans: [], stale: true };
  const spans = [];
  for (const id of edges) spans.push(...(index.edgeSpans.get(id) || []));
  for (const id of nodes) spans.push(...(index.nodeLabelSpans.get(id) || index.nodeMentionSpans.get(id) || []));
  spans.sort((x, y) => x[0] - y[0]);
  return { spans, stale: false };
}
