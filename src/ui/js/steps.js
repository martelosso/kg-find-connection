// Step-through presentation (plan §2): group a run's events into presenter-paced steps.
// DOM-free; unit-tested in Node against recorded traces.
//
// Step 0 ("setup") holds investigation.started and identity.resolved. After that:
//   - AI agent runs: each agent.decision is a "decision" step (the agent's own words and the calls
//     it planned); then EACH tool call is its own "call" step with its result and any path update,
//     so the presenter walks through the turn one call at a time. A call step knows its turn
//     (`parent`, the decision step's index) and its position in it (`callNo`).
//   - Deterministic runs: one step per expansion (tool.started). Its text is the backend's rule
//     ("route cost, degree"), labelled as such, never as AI reasoning.
//   - The terminal event is its own final "result" step.
// Only text the run actually emitted is shown: the agent's visible decision summary and backend
// facts (tool arguments, results, paths, budgets). Nothing is reconstructed or invented.

import { TERMINAL, pathsText } from "./investigation.js";

export class StepBook {
  constructor() { this.reset(); }

  reset() {
    this.steps = [];          // [{ kind: "setup" | "decision" | "expansion" | "result", events: [] }]
    this.strategy = null;     // "agent" | "deterministic", from investigation.started
    this.done = false;        // terminal event received
    this.lastEventId = 0;
    this.turn = null;         // index of the latest decision step
    this.hadPath = false;     // a path had been found before the current event
  }

  /** Add one event (in order). Returns true if it was new. */
  push(ev) {
    if (ev.event_id <= this.lastEventId) return false;  // SSE reconnects replay old events
    this.lastEventId = ev.event_id;
    const current = this.steps[this.steps.length - 1];
    const open = (kind) => { const s = { kind, events: [ev] }; this.steps.push(s); return s; };
    if (ev.type === "investigation.started") this.strategy = ev.payload.strategy;
    if (!current) open("setup");
    else if (ev.type === "agent.decision") {
      const s = open("decision");
      s.planned = ev.payload.tool_calls?.length || 0;
      s.calls = [];
      this.turn = this.steps.length - 1;
    } else if (ev.type === "tool.started" && this.strategy !== "agent") open("expansion");
    else if (ev.type === "tool.started") {
      const s = open("call");
      s.parent = this.turn;
      if (this.turn !== null) { const d = this.steps[this.turn]; d.calls.push(this.steps.length - 1); s.callNo = d.calls.length; }
    } else if (TERMINAL.has(ev.type)) { open("result"); this.done = true; }
    else current.events.push(ev);
    if (ev.type === "paths.updated") {
      const c = ev.payload.comparison;
      const has = !!(c?.shortest?.result || c?.strongest?.result);
      const step = this.steps[this.steps.length - 1];
      if (has) step.pathEvent = this.hadPath ? "improved" : "first";
      this.hadPath ||= has;
    }
    return true;
  }

  get count() { return this.steps.length; }

  /** A step can be shown once the next one has started (or the run ended): its events are all in. */
  isComplete(i) { return i < this.steps.length - 1 || (this.done && i === this.steps.length - 1); }

  /** How many calls a call step's turn has (planned by the model, or seen so far). */
  callCount(i) {
    const p = this.steps[i]?.parent;
    if (p === null || p === undefined) return 0;
    return Math.max(this.steps[p].planned || 0, this.steps[p].calls.length);
  }

  /** The calls of decision step i that have arrived so far: [{tool, label, qid, side}]. */
  plan(i) {
    return (this.steps[i]?.calls || []).map((j) => {
      const p = this.steps[j].events[0].payload;
      return { tool: p.tool, label: p.label || null, qid: p.qid || null, side: p.side || null, arguments: p.arguments || {} };
    });
  }

  /** Index of the last step that may be shown now. */
  get lastComplete() {
    for (let i = this.steps.length - 1; i >= 0; i--) if (this.isComplete(i)) return i;
    return -1;
  }
}

/** Plain description of one step for the step card. */
export function describeStep(step) {
  const out = { kind: step.kind, decision: null, rule: null, tools: [], paths: null, result: null, edgeIds: [],
                nodeIds: [], expandedQids: [], pathEvent: step.pathEvent || null };
  const byStep = new Map();
  for (const ev of step.events) {
    const p = ev.payload;
    switch (ev.type) {
      case "agent.decision":
        out.decision = p.summary || null;
        break;
      case "tool.started": {
        const t = { step: p.step, tool: p.tool, qid: p.qid || null, label: p.label || null, side: p.side || null,
                    arguments: p.arguments || {}, ok: null, error: null, returned: null, addedNodes: 0,
                    addedEdges: 0, truncated: false, capHit: null };
        byStep.set(p.step, t);
        out.tools.push(t);
        if (step.kind === "expansion" && p.reason) out.rule = p.reason;
        break;
      }
      case "graph.expanded": {
        const t = byStep.get(p.step);
        if (t) Object.assign(t, { returned: p.returned, addedNodes: p.added_nodes.length, addedEdges: p.added_edges.length,
                                  truncated: !p.complete, capHit: p.cap_hit || null });
        out.edgeIds.push(...(p.highlight_edge_ids || []));
        out.nodeIds.push(...p.added_nodes.map((n) => n.id));
        if (p.expanded_qid) out.expandedQids.push(p.expanded_qid);
        break;
      }
      case "tool.completed": {
        const t = byStep.get(p.step);
        if (t) { t.ok = p.ok; t.error = p.ok ? null : p.error || null; }
        break;
      }
      case "paths.updated":
        out.paths = pathsText(p.comparison);
        break;
      default:
        if (TERMINAL.has(ev.type)) {
          out.result = { type: ev.type, outcome: p.outcome || null, stop: p.stop_reason || null,
                         error: p.error || null, answer: p.answer?.summary || null, paths: pathsText(p.comparison) };
        }
    }
  }
  return out;
}

/** One-line description of a tool call's target, e.g. "Oliver Stone (Q179497, source side)". */
export function toolTarget(t) {
  if (t.label || t.qid) {
    const extras = [t.label && t.qid ? t.qid : null, t.side ? `${t.side} side` : null].filter(Boolean);
    return `${t.label || t.qid}${extras.length ? ` (${extras.join(", ")})` : ""}`;
  }
  const a = t.arguments || {};
  if (a.qid) return a.qid;
  if (a.raw) return "malformed arguments";
  return Object.keys(a).length ? Object.entries(a).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(" ") : "";
}

/**
 * What a tool does, in plain words, for the step card. These describe the backend tool itself (fixed
 * facts about the code), never the model's reasoning; the agent's own words are quoted separately.
 */
export function toolPurpose(t) {
  const who = t.label || t.qid || "this node";
  switch (t.tool) {
    case "get_neighbors":
    case "expand_neighbors":
      return `Fetch ${who}'s relationships from the Wikidata snapshot${t.side ? ` (grows the ${t.side} side)` : ""}.`;
    case "compute_paths":
      return "Ask the backend to run its shortest- and strongest-path searches over everything explored so far.";
    case "get_search_status":
      return "Ask the backend how much budget is left and which explored people it ranks as most promising to expand next.";
    case "inspect_path":
      return "Read the hop-by-hop details (relations and costs) of a path the backend found.";
    case "resolve_person":
      return "Look up a person's Wikidata ID.";
    case "finish":
      return "Stop exploring and hand over to the backend-verified result.";
    default:
      return "";
  }
}

const CIRCLED = (n) => n >= 1 && n <= 20 ? String.fromCodePoint(0x2460 + n - 1)
  : n <= 35 ? String.fromCodePoint(0x3251 + n - 21) : n <= 50 ? String.fromCodePoint(0x32B1 + n - 36) : `(${n})`;

/** Exploration-order badge for the n-th expanded node: ① ② ③ … */
export function orderBadge(n) { return CIRCLED(n); }
