// The people-connections ontology as a schema diagram (no DOM; unit-tested with Node).
//
// The server sends /api/people-ontology: classes, relation groups and properties from
// ontology/people_connections.ttl (meaning), plus each property's strength and each group's default
// on/off from config/relationships.yaml (weights). Here that becomes:
//   - schemaGraph(): one diagram node per class and ONE ARROW PER (domain, range, relation group), so
//     ~10 readable arrows instead of 26 overlapping ones;
//   - ontologyIndex(): each subject block of the Turtle text (ex:Person, wdt:P108, ex:family) mapped
//     to its source span, for the same click <-> highlight link the data Turtle has. Like mapping.js
//     it refuses to answer for a text it did not parse (the stale-offset guard).

import { parseTurtle } from "./turtle-parser.js";
import { strengthWord } from "./steps.js";

export const NS = {
  ex: "https://example.org/six-degrees/ontology#",
  wdt: "http://www.wikidata.org/prop/direct/",
};

/** Where each class sits in the diagram (fixed: the picture never moves). Person is the hub. */
export const LAYOUT = {
  Person: { x: 0, y: 40 },
  Organization: { x: -460, y: 190 },
  Place: { x: 460, y: 190 },
  Work: { x: 0, y: 400 },
  Award: { x: -300, y: 420 },
  Position: { x: 300, y: 420 },
};
/** Person -> Person relations point at a second Person node above (a loop on one node is unreadable). */
export const OTHER_PERSON = { id: "Person#2", x: 0, y: -400 };

/** "Birthplace (weak)" -> "Birthplace": the diagram shows weakness as a thin, pale arrow instead. */
export const shortGroup = (label) => String(label || "").replace(/\s*\(weak\)\s*$/i, "");

const arrowId = (domain, range, group) => `${domain}>${range}:${group}`;

/** Wrap property names into short lines ("cast member · director" / "screenwriter · composer"). */
function wrapNames(names, width = 26) {
  const lines = [];
  let cur = "";
  for (const n of names) {
    const next = cur ? `${cur} · ${n}` : n;
    if (cur && next.length > width) { lines.push(cur); cur = n; } else cur = next;
  }
  if (cur) lines.push(cur);
  return lines;
}

/**
 * The diagram: `enabled` is the set of relation groups on for this run (null = the policy defaults);
 * `used` the PIDs the current strongest path walks (lit in the path colour).
 */
export function schemaGraph(onto, enabled = null, used = new Set()) {
  const on = enabled ? new Set(enabled) : new Set(onto.groups.filter((g) => g.enabled_by_default).map((g) => g.id));
  const groups = new Map(onto.groups.map((g) => [g.id, g]));
  const classes = new Map(onto.classes.map((c) => [c.id, c]));
  const arrows = new Map();
  for (const p of onto.properties) {
    const id = arrowId(p.domain, p.range, p.group);
    if (!arrows.has(id)) {
      const g = groups.get(p.group) || { id: p.group, label: p.group, link_type: "" };
      arrows.set(id, { id, from: p.domain, to: p.range, group: p.group, groupLabel: shortGroup(g.label),
                       linkType: g.link_type, linkLabel: g.link_label || "", enabled: on.has(p.group),
                       properties: [], strength: 0, used: false });
    }
    const a = arrows.get(id);
    a.properties.push(p);
    a.strength = Math.max(a.strength, p.strength);
    if (used.has(p.pid)) a.used = true;
  }
  for (const a of arrows.values()) {
    a.word = strengthWord(a.strength);
    a.lines = [a.groupLabel, ...wrapNames(a.properties.map((p) => p.label))];
  }
  const nodes = onto.classes.map((c) => ({ ...c, ...(LAYOUT[c.id] || { x: 0, y: 0 }),
    arrows: [...arrows.values()].filter((a) => a.from === c.id || a.to === c.id).length }));
  return { nodes, arrows: [...arrows.values()], classes, enabled: on };
}

/** The diagram arrow a property belongs to, in words: Work —cast member→ Person, Shared film / music. */
export function ruleFor(onto, pid) {
  const p = onto?.properties.find((x) => x.pid === pid);
  if (!p) return null;
  const g = onto.groups.find((x) => x.id === p.group) || {};
  const cls = (id) => onto.classes.find((c) => c.id === id)?.label || id;
  return { pid, label: p.label, inverseLabel: p.inverse_label, group: p.group, groupLabel: shortGroup(g.label),
           domain: p.domain, range: p.range, domainLabel: cls(p.domain), rangeLabel: cls(p.range),
           linkType: g.link_type, linkLabel: g.link_label || "", strength: p.strength, word: strengthWord(p.strength),
           arrowId: arrowId(p.domain, p.range, p.group) };
}

/** Distinct rules (in path order) for a list of PIDs, e.g. the hops of the strongest path. */
export function rulesFor(onto, pids) {
  const seen = new Set(), out = [];
  for (const pid of pids) {
    if (seen.has(pid)) continue;
    seen.add(pid);
    const r = ruleFor(onto, pid);
    if (r) out.push(r);
  }
  return out;
}

const localName = (iri) => {
  for (const [prefix, ns] of Object.entries(NS)) if (iri.startsWith(ns)) return { prefix, id: iri.slice(ns.length) };
  return null;
};

/** Parse the ontology text: subject block spans by kind ("class" | "group" | "property"). Throws on bad syntax. */
export function ontologyIndex(text, onto) {
  const classIds = new Set(onto.classes.map((c) => c.id)), groupIds = new Set(onto.groups.map((g) => g.id));
  const blocks = [];  // {kind, id, span}
  const seen = new Set();
  const triples = parseTurtle(text);
  for (const t of triples) {
    const key = `${t.stmt[0]}:${t.stmt[1]}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const n = t.subj.type === "iri" ? localName(t.subj.value) : null;
    if (!n) continue;
    const kind = n.prefix === "wdt" ? "property" : classIds.has(n.id) ? "class" : groupIds.has(n.id) ? "group" : null;
    if (kind) blocks.push({ kind, id: n.id, span: t.stmt });
  }
  return { text, triples: triples.length, blocks };
}

/** Diagram selection {classes, arrows} -> source spans (an arrow: its properties' blocks + its group's). */
export function schemaToSpans(index, schema, { classes = [], arrows = [] }, currentText) {
  if (!index || index.text !== currentText) return { spans: [], stale: true };
  const want = new Set(classes.map((c) => `class:${c}`));
  for (const id of arrows) {
    const a = schema.arrows.find((x) => x.id === id);
    if (!a) continue;
    a.properties.forEach((p) => want.add(`property:${p.pid}`));
    want.add(`group:${a.group}`);
  }
  const spans = index.blocks.filter((b) => want.has(`${b.kind}:${b.id}`)).map((b) => b.span).sort((x, y) => x[0] - y[0]);
  return { spans, stale: false };
}

/** Editor selection ranges -> the classes and arrows whose blocks they overlap. */
export function selectionToSchema(index, schema, ranges, currentText) {
  const classes = new Set(), arrows = new Set();
  if (!index || index.text !== currentText) return { classes, arrows, stale: true };
  for (const b of index.blocks) {
    if (!ranges.some(([a, z]) => b.span[0] < z && b.span[1] > a)) continue;
    if (b.kind === "class") classes.add(b.id);
    else if (b.kind === "property") schema.arrows.filter((a) => a.properties.some((p) => p.pid === b.id)).forEach((a) => arrows.add(a.id));
    else schema.arrows.filter((a) => a.group === b.id).forEach((a) => arrows.add(a.id));
  }
  return { classes, arrows, stale: false };
}
