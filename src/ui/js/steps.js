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
//   - Once the run has ended, the steps after the strongest path last changed (the search for a
//     stronger path that found none) are folded into ONE "tail" step (`foldTail`), so the demo goes
//     from the connection straight to "looked further, nothing stronger" to the result. The presenter
//     can unfold it (`unfoldTail`) to walk through those calls one by one.
// Only text the run actually emitted is shown: the agent's visible decision summary and backend
// facts (tool arguments, results, paths, budgets). Nothing is reconstructed or invented.

import { TERMINAL, pathsText, plainWords } from "./investigation.js";

export class StepBook {
  constructor() { this.reset(); }

  reset() {
    this.steps = [];          // [{ kind: "setup" | "decision" | "expansion" | "result", events: [] }]
    this.strategy = null;     // "agent" | "deterministic", from investigation.started
    this.done = false;        // terminal event received
    this.lastEventId = 0;
    this.turn = null;         // index of the latest decision step
    this.pathId = null;       // the strongest path shown so far (path_id), null before any path
    this.folded = null;       // the "tail" step while the tail is folded
    this.unfolded = false;    // the presenter unfolded it: never fold again for this run
    this.path = null;         // the strongest PathResult shown so far
    this.sideOf = new Map();  // qid -> "source" | "target": which person's side first reached it
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
      // Step objects, not indices: folding the tail shifts the indices of later steps.
      if (this.turn !== null) { const d = this.steps[this.turn]; d.calls.push(s); s.callNo = d.calls.length; }
    } else if (TERMINAL.has(ev.type)) { open("result"); this.done = true; }
    else current.events.push(ev);
    if (ev.type === "identity.resolved") {
      this.sideOf.set(ev.payload.source.id, "source");
      this.sideOf.set(ev.payload.target.id, "target");
    } else if (ev.type === "graph.expanded") {
      for (const n of ev.payload.added_nodes || []) if (!this.sideOf.has(n.id)) this.sideOf.set(n.id, ev.payload.side);
    }
    if (ev.type === "paths.updated") {
      const c = ev.payload.comparison;
      // The demo shows the strongest path only, so a step "improves" the path only when the strongest
      // path changes (paths.updated also fires when just the shortest one changed).
      const r = c?.strongest?.result || null;
      const step = this.steps[this.steps.length - 1];
      if (r && r.path_id !== this.pathId) {
        step.pathEvent = this.pathId ? "improved" : "first";
        // What the path step card needs to explain the moment: the new path, the one it replaced, and
        // which side had reached each node by then (to say where the two sides met).
        step.pathResult = r;
        step.prevPath = this.path;
        step.sides = new Map(this.sideOf);
      }
      if (r) { this.pathId = r.path_id; this.path = r; }
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
    return (this.steps[i]?.calls || []).map((s) => {
      const p = s.events[0].payload;
      return { tool: p.tool, label: p.label || null, qid: p.qid || null, side: p.side || null, arguments: p.arguments || {} };
    });
  }

  /**
   * The ranked suggestions of the latest get_search_status call at or before step i, exactly as the
   * agent was shown them ({step, suggestions: [{rank, qid, label, side, hops, degree, score}], total}),
   * or null (none yet, or a recording made before suggestions were recorded).
   */
  lastSuggestions(i) {
    for (let j = Math.min(i, this.steps.length - 1); j >= 0; j--) {
      for (const ev of this.steps[j].events) {
        const p = ev.payload;
        if (ev.type === "tool.completed" && p.tool === "get_search_status" && p.ok && Array.isArray(p.suggestions)) {
          return { step: j, suggestions: p.suggestions, total: p.frontier_size ?? null };
        }
      }
    }
    return null;
  }

  /**
   * The ranking the agent had in front of it when it made decision step i: the suggestions of a
   * get_search_status call in an EARLIER turn (calls of one turn are issued together, so a call can't
   * have seen its sibling's result), provided nothing new was added to the graph since (otherwise the
   * ranking no longer describes the graph the agent chose from). For a call step, its turn's decision.
   */
  rankingFor(i) {
    const s = this.steps[i];
    if (!s) return null;
    if (s.kind === "call") return s.parent === null || s.parent === undefined ? null : this.rankingFor(s.parent);
    if (s.kind !== "decision") return null;
    for (let j = i - 1; j >= 0; j--) {
      for (const ev of this.steps[j].events) {
        const p = ev.payload;
        if (ev.type === "graph.expanded" && (p.added_nodes?.length || p.added_edges?.length)) return null;  // stale
        if (ev.type === "tool.completed" && p.tool === "get_search_status" && p.ok && Array.isArray(p.suggestions)) {
          return { step: j, suggestions: p.suggestions, total: p.frontier_size ?? null };
        }
      }
    }
    return null;
  }

  /**
   * Fold the steps after the strongest path last changed (and after step `after`, the one on screen,
   * so nothing the presenter already saw moves) into one "tail" step, if there are at least MIN_FOLD.
   * Only once the run has ended with a path. Returns true if it folded.
   */
  foldTail(after = 0) {
    if (!this.done || this.folded || this.unfolded || this.pathId === null) return false;
    const r = this.steps.length - 1;  // the result step
    let p = -1;
    for (let i = r - 1; i >= 0; i--) if (this.steps[i].pathEvent) { p = i; break; }
    const from = Math.max(p, after) + 1;
    if (p < 0 || r - from < MIN_FOLD) return false;
    const members = this.steps.slice(from, r);
    const tail = { kind: "tail", events: members.flatMap((s) => s.events), members, pathStep: p };
    this.steps.splice(from, members.length, tail);
    this.folded = tail;
    return true;
  }

  /** Put the folded steps back in place. Returns the index of the first of them, or -1. */
  unfoldTail() {
    const tail = this.folded;
    if (!tail) return -1;
    const i = this.steps.indexOf(tail);
    this.steps.splice(i, 1, ...tail.members);
    this.folded = null;
    this.unfolded = true;
    return i;
  }

  /**
   * The improvement leads the agent had in front of it when it made decision step i: the latest
   * tool.leads event in an EARLIER turn, unless the graph grew after it (then it no longer describes
   * the graph the agent chose from). For a call step, its turn's decision. Null when none recorded.
   */
  leadsFor(i) {
    const s = this.steps[i];
    if (!s) return null;
    if (s.kind === "call") return s.parent === null || s.parent === undefined ? null : this.leadsFor(s.parent);
    if (s.kind !== "decision") return null;
    for (let j = i - 1; j >= 0; j--) {
      const evs = this.steps[j].events;
      for (let k = evs.length - 1; k >= 0; k--) {
        const ev = evs[k], p = ev.payload;
        if (ev.type === "tool.leads") return { step: j, leads: p.improvement_leads || [], more: p.more_leads || 0, possible: p.stronger_path_possible };
        if (ev.type === "graph.expanded" && (p.added_nodes?.length || p.added_edges?.length)) return null;
      }
    }
    return null;
  }

  /** The latest recorded leads at or before step i (any turn), or null. */
  latestLeads(i) {
    for (let j = Math.min(i, this.steps.length - 1); j >= 0; j--) {
      const evs = this.steps[j].events;
      for (let k = evs.length - 1; k >= 0; k--) {
        if (evs[k].type === "tool.leads") return evs[k].payload.improvement_leads || [];
      }
    }
    return null;
  }

  /**
   * Tool guidance the agent DID receive before decision step i that this recording did NOT store, so
   * the card must not let the agent take the credit: "leads" (a path existed, so tool results carried
   * improvement leads) or "ranking" (an earlier get_search_status returned a ranked list). Else null.
   */
  unrecordedGuidance(i) {
    const s = this.steps[i];
    if (!s || s.kind !== "decision" || this.strategy !== "agent") return null;
    const before = this.steps.slice(0, i);
    const recorded = (type) => this.steps.some((x) => x.events.some((e) => type(e)));
    if (before.some((x) => x.pathEvent) && !recorded((e) => e.type === "tool.leads")) return "leads";
    // A ranking only informs the next turn: look at the calls of the turn just before this one.
    let status = false;
    for (let j = i - 1; j >= 0 && this.steps[j].kind !== "decision"; j--) {
      status ||= this.steps[j].events.some((e) => e.type === "tool.completed" && e.payload.tool === "get_search_status" && e.payload.ok);
    }
    if (status && !recorded((e) => e.type === "tool.completed" && Array.isArray(e.payload.suggestions))) return "ranking";
    return null;
  }

  /** Index of the last step that may be shown now. */
  get lastComplete() {
    for (let i = this.steps.length - 1; i >= 0; i--) if (this.isComplete(i)) return i;
    return -1;
  }
}

/** Fewest steps after the last path change that are worth folding into one. */
export const MIN_FOLD = 3;

/**
 * What a folded tail did, from its recorded events only: {steps, expansions: [{qid, label, side,
 * addedNodes, lead}], otherCalls: {tool: count}, reason (the agent's words when it chose to keep
 * looking, or null), addedNodes, addedEdges, fromLeads}. `leadsBefore` is the improvement-lead list
 * recorded just before the tail (book.latestLeads); `fromLeads` counts the expansions that were on the
 * latest list at the time, null when no list was recorded.
 */
export function describeTail(step, leadsBefore = null) {
  const started = new Map(), expansions = [], otherCalls = {}, isLead = new Set();
  let addedNodes = 0, addedEdges = 0, reason = null, reasonVerbatim = null, leads = leadsBefore, fromLeads = leadsBefore ? 0 : null;
  for (const ev of step.events) {
    const p = ev.payload;
    if (ev.type === "agent.decision" && !reason && p.summary && p.tool_calls?.length) { reason = plainWords(tidyClip(p.summary)); reasonVerbatim = p.summary; }
    else if (ev.type === "tool.leads") { leads = p.improvement_leads || []; fromLeads ??= 0; }
    else if (ev.type === "tool.started") {
      started.set(p.step, p);
      if (leads && p.qid && leads.some((l) => l.qid === p.qid)) { isLead.add(p.step); fromLeads += 1; }
    }
    else if (ev.type === "graph.expanded") {
      const s = started.get(p.step) || {};
      expansions.push({ qid: p.expanded_qid, label: s.label || p.expanded_qid, side: p.side || s.side || null,
                        addedNodes: p.added_nodes.length, lead: isLead.has(p.step) });
      addedNodes += p.added_nodes.length;
      addedEdges += p.added_edges.length;
      started.delete(p.step);
    }
  }
  for (const s of started.values()) otherCalls[s.tool] = (otherCalls[s.tool] || 0) + 1;
  return { steps: step.members.length, expansions, otherCalls, reason, reasonVerbatim, addedNodes, addedEdges, fromLeads };
}

/**
 * Older recordings cut the model's message at exactly 300 characters, often mid-word ("institutio").
 * Show such a message up to its last full word with an ellipsis, so the cut is visible and honest.
 */
export function tidyClip(text, limit = 300) {
  if (!text || text.length < limit || /[.!?…"”')\]]$/.test(text)) return text;
  return `${text.replace(/\s+\S*$/, "").replace(/[\s,;:—-]+$/, "")}…`;
}

/** Plain description of one step for the step card. */
export function describeStep(step) {
  const out = { kind: step.kind, decision: null, rule: null, tools: [], paths: null, result: null, edgeIds: [],
                nodeIds: [], expandedQids: [], pathEvent: step.pathEvent || null,
                pathResult: step.pathResult || null, prevPath: step.prevPath || null, sides: step.sides || null };
  const byStep = new Map();
  for (const ev of step.events) {
    const p = ev.payload;
    switch (ev.type) {
      case "agent.decision":
        out.decision = plainWords(tidyClip(p.summary || null));
        out.decisionVerbatim = p.summary || null;
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
        if (t) {
          t.ok = p.ok; t.error = p.ok ? null : p.error || null;
          if (Array.isArray(p.suggestions)) { t.suggestions = p.suggestions; t.frontierSize = p.frontier_size ?? null; }
        }
        break;
      }
      case "paths.updated":
        out.paths = pathsText(p.comparison);
        break;
      default:
        if (TERMINAL.has(ev.type)) {
          out.result = { type: ev.type, outcome: p.outcome || null, stop: p.stop_reason || null,
                         error: p.error || null, answer: plainWords(p.answer?.summary || null), answerVerbatim: p.answer?.summary || null,
                         answerSource: p.answer?.summary_source || null, paths: pathsText(p.comparison),
                         comparison: p.comparison || null };
        }
    }
  }
  return out;
}

/** Stop reasons in plain words for the result card. */
const STOP_WORDS = {
  agent_finished: "the agent decided to stop", path_found: "a path was found and checked",
  frontier_exhausted: "nothing left to explore", max_explored_nodes: "node limit reached",
  max_explored_edges: "edge limit reached", max_provider_requests: "request limit reached",
  max_agent_tool_calls: "tool-call limit reached", max_turns: "turn limit reached",
  wall_time_seconds: "time limit reached", no_progress: "the agent stopped making progress",
  agent_error: "the AI model failed", cancelled: "cancelled by the presenter",
};

/**
 * The final step's headline, honest about how the run ended: {head, tone, scope}. A path found by a
 * run that hit a budget is still a connection found, but the scope line says the search stopped early
 * (something shorter or stronger may lie beyond what was explored).
 */
export function resultHeadline(r) {
  const found = r?.outcome === "path_found";
  const stop = STOP_WORDS[r?.stop] || String(r?.stop || "unknown").replaceAll("_", " ");
  const partial = r?.type === "investigation.partial";
  if (r?.type === "investigation.failed" && !found) {
    return { head: "Run failed", tone: "err", scope: r?.error ? `${r.error.code}: ${r.error.message}` : `stop: ${stop}` };
  }
  const among = "Within the explored subgraph only, not all of Wikidata.";
  return {
    head: found ? "Connection found" : partial ? "No connection found yet" : "No connection found",
    tone: found ? "ok" : "warn",
    scope: partial ? `The search stopped early (${stop}), so a shorter or stronger connection may lie beyond what was explored. ${among}`
      : `Search finished (${stop}). ${among}`,
  };
}

/**
 * The strongest path of a comparison as a chain for the result card (the demo shows only the strongest
 * path; the backend still computes the shortest). Each chain: {mode: "strongest", hops, cost,
 * nodes: [{qid, label, kind, role: "source" | "target" | null}], rels: [{label, pid, group, reverse}]}
 * (rels[i] links nodes[i] to nodes[i+1]; labels are already direction-aware, e.g. "acted in").
 */
export function pathChains(c) {
  const w = c?.strongest?.result;
  return w ? [pathChain(w, "strongest")] : [];
}

/** One PathResult as a chain (see pathChains); rels also carry the policy strength and its word. */
export function pathChain(r, mode = "strongest") {
  const byQid = new Map((r.nodes || []).map((n) => [n.qid, n]));
  const qids = r.hops.length ? [r.hops[0].from_qid, ...r.hops.map((h) => h.to_qid)] : [r.source_qid];
  return {
    mode, hops: r.hop_count, cost: r.total_cost, pathId: r.path_id,
    hubPenalty: r.hops.reduce((s, h) => s + (h.cost?.hub_penalty || 0), 0),
    nodes: qids.map((q) => {
      const n = byQid.get(q) || {};
      return { qid: q, label: n.label_en || n.label_es || q, kind: n.kind || "other",
               role: q === r.source_qid ? "source" : q === r.target_qid ? "target" : null };
    }),
    rels: r.hops.map((h) => ({ label: h.label || h.pid, pid: h.pid, group: h.relation_group, edgeId: h.edge_id,
                               strength: h.cost?.strength ?? null, word: strengthWord(h.cost?.strength),
                               reverse: h.direction === "reverse", sandbox: h.origin === "sandbox" })),
  };
}

/**
 * The policy strength of a relation in one word, for people: "strong" (>= 0.8: spouse, parent, child,
 * sibling, teacher/student), "medium" (>= 0.5: relative, a shared film or record), "weak" (below: a
 * shared school, employer, team, organisation, birthplace...). Thresholds follow config/relationships.yaml.
 */
export function strengthWord(s) {
  if (s === null || s === undefined) return null;
  return s >= 0.8 ? "strong" : s >= 0.5 ? "medium" : "weak";
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The chain's links by strength word: "2 strong links (spouse, child) · 2 weak (educated at, alma mater of)". */
export function strengthSummary(chain) {
  const by = { strong: [], medium: [], weak: [] };
  for (const r of chain.rels) if (r.word) by[r.word].push(r.label);
  return ["strong", "medium", "weak"].filter((w) => by[w].length).map((w, i) => {
    const n = by[w].length, labels = [...new Set(by[w])].join(", ");
    return i === 0 ? `${n} ${w} link${n === 1 ? "" : "s"} (${labels})` : `${n} ${w} (${labels})`;
  }).join(" · ");
}

/**
 * The honesty rule for a chain that runs through something shared rather than a person-to-person
 * link: "Through United Nations: a shared organisation is an association, not proof that they met."
 * Null when every intermediate entity is a person.
 */
const SHARED = { organization: "organisation", work: "film or record", place: "birthplace", position: "position" };
export function associationNote(chain) {
  const mids = (chain?.nodes || []).slice(1, -1).filter((n) => n.kind !== "person");
  if (!mids.length) return null;
  const kinds = [...new Set(mids.map((n) => SHARED[n.kind] || "entity"))];
  const what = kinds.length === 1 ? `a shared ${kinds[0]}` : "a shared organisation, work or place";
  return `Through ${mids.map((n) => n.label).join(", ")}: ${what} is an association, not proof that they met.`;
}

/**
 * Why `next` beats `prev` (both chains), in one plain sentence built only from the paths' own hop
 * strengths and hub penalties (the terms of the policy's weighted cost).
 */
export function whyStronger(prev, next) {
  const count = (c, w) => c.rels.filter((r) => r.word === w).length;
  const d = next.hops - prev.hops;
  const hops = d === 0 ? `Same number of hops (${next.hops})` : d > 0 ? `${plural(d, "hop")} longer` : `${plural(-d, "hop")} shorter`;
  let reason;
  if (count(next, "strong") > count(prev, "strong")) reason = `${plural(count(next, "strong"), "strong link")} instead of ${count(prev, "strong")}`;
  else if (count(next, "weak") < count(prev, "weak")) reason = `${plural(count(next, "weak"), "weak link")} instead of ${count(prev, "weak")}`;
  else if (next.hubPenalty < prev.hubPenalty - 1e-9) reason = "it goes through a smaller shared organisation, and a small shared school or employer says more than a huge one";
  else if (d < 0) reason = "it needs fewer steps";
  else reason = "its links add up to a lower weighted cost";
  return `${hops}, ${d > 0 ? "but" : "and"} ${reason}.`;
}

/**
 * Where the two people's sides met on a path that just appeared: the first hop whose ends were first
 * reached from different sides. Returns {meet, already: "source" | "target", from, rel, to,
 * viaThisStep} or null. `meet` is the end the OTHER side had reached (the expanded node's side found
 * it now); `viaThisStep` says whether the joining statement was added by this step.
 */
export function meetingPoint(chain, sides, expandedQid, stepEdgeIds = []) {
  if (!chain || !sides) return null;
  const mine = sides.get(expandedQid) || null;
  for (let i = 0; i < chain.rels.length; i++) {
    const a = chain.nodes[i], b = chain.nodes[i + 1];
    const sa = sides.get(a.qid), sb = sides.get(b.qid);
    if (!sa || !sb || sa === sb) continue;
    const meet = mine && sb !== mine ? b : mine && sa !== mine ? a : b;
    return { meet, already: sides.get(meet.qid), from: a, rel: chain.rels[i], to: b,
             viaThisStep: stepEdgeIds.includes(chain.rels[i].edgeId) };
  }
  return null;
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
 * toolAction is the short form ("Expand Neymar"), toolPurpose the full sentence.
 */
export function toolAction(t) {
  const who = t.label || t.qid || t.arguments?.qid || "a node";
  switch (t.tool) {
    case "get_neighbors":
    case "expand_neighbors": return `Expand ${who}`;
    case "compute_paths": return "Run the path search";
    case "get_search_status": return "Check the budget and the frontier ranking";
    case "inspect_path": return "Inspect a path";
    case "resolve_person": return `Look up ${t.arguments?.name || who}`;
    case "finish": return "Finish";
    default: return t.tool || "Tool call";
  }
}

export function toolPurpose(t, sideName = (s) => `the ${s} side`) {
  const who = t.label || t.qid || "this node";
  switch (t.tool) {
    case "get_neighbors":
    case "expand_neighbors":
      return `Tool: one-hop neighbourhood query on the local Wikidata snapshot, returning ${who}'s statements in the enabled relation groups${t.side ? ` (grows ${sideName(t.side)})` : ""}.`;
    case "compute_paths":
      return "Tool: runs the deterministic path search (weighted, hop-limited) over the explored subgraph.";
    case "get_search_status":
      return "Tool: returns the remaining budget and the frontier ranking: unexpanded entities ordered by how promising they are to expand.";
    case "inspect_path":
      return "Tool: returns the hop-by-hop statements (properties and costs) of a verified path.";
    case "resolve_person":
      return "Tool: resolves a name to a Wikidata entity (QID).";
    case "finish":
      return "Tool: ends the agent loop; the final answer must pass the grounding check against the verified path.";
    default:
      return "";
  }
}

const CIRCLED = (n) => n >= 1 && n <= 20 ? String.fromCodePoint(0x2460 + n - 1)
  : n <= 35 ? String.fromCodePoint(0x3251 + n - 21) : n <= 50 ? String.fromCodePoint(0x32B1 + n - 36) : `(${n})`;

/** Exploration-order badge for the n-th expanded node: ① ② ③ … */
export function orderBadge(n) { return CIRCLED(n); }
