// Step-through presentation (plan §2): group a run's events into presenter-paced steps.
// DOM-free; unit-tested in Node against recorded traces.
//
// Step 0 ("setup") holds investigation.started and identity.resolved. After that:
//   - AI agent runs: a step starts at each agent.decision and holds the tool calls it led to, with
//     their results and any path update. A tool call that arrives before any decision text starts
//     its own step (the model may call tools without writing anything).
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
  }

  /** Add one event (in order). Returns true if it was new. */
  push(ev) {
    if (ev.event_id <= this.lastEventId) return false;  // SSE reconnects replay old events
    this.lastEventId = ev.event_id;
    const current = this.steps[this.steps.length - 1];
    const open = (kind) => { const s = { kind, events: [ev] }; this.steps.push(s); return s; };
    if (ev.type === "investigation.started") this.strategy = ev.payload.strategy;
    if (!current) open("setup");
    else if (ev.type === "agent.decision") open("decision");
    else if (ev.type === "tool.started" && this.strategy !== "agent") open("expansion");
    else if (ev.type === "tool.started" && current.kind === "setup") open("decision");
    else if (TERMINAL.has(ev.type)) { open("result"); this.done = true; }
    else current.events.push(ev);
    return true;
  }

  get count() { return this.steps.length; }

  /** A step can be shown once the next one has started (or the run ended): its events are all in. */
  isComplete(i) { return i < this.steps.length - 1 || (this.done && i === this.steps.length - 1); }

  /** Index of the last step that may be shown now. */
  get lastComplete() {
    for (let i = this.steps.length - 1; i >= 0; i--) if (this.isComplete(i)) return i;
    return -1;
  }
}

/** Plain description of one step for the step card. */
export function describeStep(step) {
  const out = { kind: step.kind, decision: null, rule: null, tools: [], paths: null, result: null, edgeIds: [] };
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
