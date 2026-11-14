// Investigation state reducer (no DOM; unit-tested with Node by replaying recorded event traces).
//
// `apply(event)` folds one plan §7 event into the model and returns a small change record the views
// use to update incrementally (the graph view never rebuilds the network from scratch).

export const TERMINAL = new Set(["investigation.completed", "investigation.partial", "investigation.failed"]);
export const EVENT_TYPES = ["investigation.started", "identity.resolved", "agent.decision", "tool.started",
  "tool.completed", "tool.leads", "graph.expanded", "paths.updated", ...TERMINAL];  // must list every type the server emits (SSE listens per type)

export class InvestigationModel {
  constructor() { this.reset(); }

  reset() {
    this.id = null;
    this.meta = null;          // investigation.started payload
    this.source = null;        // node payloads
    this.target = null;
    this.nodes = new Map();    // qid -> node payload (+ order)
    this.edges = new Map();    // edge id -> edge payload
    this.expanded = new Set(); // QIDs whose neighbours were fetched
    this.timeline = [];        // rows for the timeline view
    this.comparison = null;
    this.final = null;         // terminal event payload (+ type)
    this.status = "idle";      // idle | running | completed | partial | failed
    this.lastEventId = 0;
    this._steps = new Map();   // step -> timeline row
    this._decision = null;     // current agent.decision row (tool calls nest under it)
  }

  get isReplay() { return this.meta?.run_mode === "replay"; }
  get isAgent() { return this.meta?.strategy === "agent"; }

  addNodes(list) {
    const added = [];
    for (const n of list || []) {
      if (this.nodes.has(n.id)) continue;
      this.nodes.set(n.id, { ...n, order: this.nodes.size });
      added.push(n.id);
    }
    return added;
  }

  addEdges(list) {
    const added = [];
    for (const e of list || []) {
      if (this.edges.has(e.id)) continue;
      this.edges.set(e.id, e);
      added.push(e.id);
    }
    return added;
  }

  pathEdgeIds(which = "both") {
    const c = this.comparison;
    const ids = (o) => (o?.result ? o.result.hops.map((h) => h.edge_id) : []);
    return {
      shortest: which === "strongest" ? [] : ids(c?.shortest),
      strongest: which === "shortest" ? [] : ids(c?.strongest),
    };
  }

  pathNodeIds() {
    const c = this.comparison, out = new Set();
    c?.strongest?.result?.nodes.forEach((n) => out.add(n.qid));  // the demo shows the strongest path only
    return out;
  }

  apply(ev) {
    if (ev.event_id <= this.lastEventId) return { type: "duplicate" };  // reconnect replays are ignored
    this.lastEventId = ev.event_id;
    this.id = ev.investigation_id;
    const p = ev.payload;
    const change = { type: ev.type, addedNodes: [], addedEdges: [], row: null };
    const row = (r) => { const full = { eventId: ev.event_id, edgeIds: [], ...r }; this.timeline.push(full); change.row = full; return full; };

    switch (ev.type) {
      case "investigation.started":
        this.meta = p; this.status = "running";
        row({ kind: "start", text: `Started · ${p.strategy === "agent" ? "LLM agent" : "deterministic baseline"} · groups: ${p.groups.join(", ")} · max ${p.max_hops} hops` });
        break;
      case "identity.resolved":
        this.source = p.source; this.target = p.target;
        change.addedNodes = this.addNodes(p.added_nodes);
        row({ kind: "resolved", text: `Resolved ${label(p.source)} (${p.source.id}) and ${label(p.target)} (${p.target.id})` });
        break;
      case "agent.decision":
        this._decision = row({ kind: "decision", text: p.summary, turn: p.turn, children: [] });
        break;
      case "tool.started": {
        const r = row({ kind: "tool", step: p.step, tool: p.tool, qid: p.qid || null, label: p.label || null,
                        side: p.side || null, reason: p.reason || null, arguments: p.arguments || {},
                        status: "running", parent: this.isAgent ? this._decision?.eventId ?? null : null });
        this._steps.set(p.step, r);
        break;
      }
      case "graph.expanded": {
        change.addedNodes = this.addNodes(p.added_nodes);
        change.addedEdges = this.addEdges(p.added_edges);
        if (p.expanded_qid) this.expanded.add(p.expanded_qid);
        const r = this._steps.get(p.step);
        if (r) Object.assign(r, { returned: p.returned, addedNodeCount: p.added_nodes.length,
                                  addedEdgeCount: p.added_edges.length, complete: p.complete,
                                  truncated: !p.complete, capHit: p.cap_hit, edgeIds: p.highlight_edge_ids });
        change.row = r || null;
        break;
      }
      case "tool.completed": {
        const r = this._steps.get(p.step);
        if (r) { r.status = p.ok ? "ok" : "error"; if (!p.ok) r.error = p.error; }
        change.row = r || null;
        break;
      }
      case "paths.updated":
        this.comparison = p.comparison;
        row({ kind: "paths", text: pathsText(p.comparison), edgeIds: this.pathEdgeIds("strongest").strongest });
        break;
      default:
        if (TERMINAL.has(ev.type)) {
          this.final = { type: ev.type, ...p };
          if (p.comparison) this.comparison = p.comparison;
          this.status = ev.type === "investigation.completed" ? "completed" : ev.type === "investigation.partial" ? "partial" : "failed";
          const text = this.status === "failed" ? `Failed: ${p.error?.code} — ${p.error?.message}`
            : `${this.status === "completed" ? "Completed" : "Partial result"}: ${p.outcome === "path_found" ? "connection found" : "no connection found"} · stop: ${p.stop_reason}`;
          row({ kind: "final", text, status: this.status });
        }
    }
    return change;
  }
}

export function label(n) { return n?.label_en || n?.label_es || n?.label || n?.id || n?.qid || "?"; }

export function pathsText(c) {
  const w = c?.strongest?.result;  // the demo shows the strongest path only
  if (!w) return "No path in the explored subgraph yet";
  return `Strongest path: ${w.hop_count} hop${w.hop_count === 1 ? "" : "s"} · cost ${w.total_cost.toFixed(2)}`;
}
