// vis-network adapter. ONE vis.Network for the page's lifetime: events only add to / update its
// DataSets, so positions, selection and camera survive every update (plan §8).
//
// Visible-node cap: beyond `maxVisible`, the least important nodes are hidden (endpoints, path
// nodes, expanded nodes and nodes linking 2+ visible nodes are always kept). "Show all" lifts it.

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export class GraphView {
  constructor(container, { maxVisible = 250, onSelect } = {}) {
    this.maxVisible = maxVisible;
    this.labelThreshold = 60;  // above this many nodes, only important nodes keep a text label
    this.showAll = false;
    this.colors = {
      source: css("--c-source"), target: css("--c-target"), person: css("--c-person"),
      organization: css("--c-org"), position: css("--c-position"), other: css("--c-other"),
      work: css("--c-work"), place: css("--c-place"),
      edge: css("--c-edge"), dim: css("--c-dim"), shortest: css("--c-shortest"), strongest: css("--c-strongest"),
      fresh: css("--c-fresh"), sandbox: css("--c-sandbox"), select: css("--c-select"),
    };
    this.nodesDS = new vis.DataSet();
    this.edgesDS = new vis.DataSet();
    this.nodes = new Map();   // id -> payload
    this.edges = new Map();   // id -> payload
    this.hidden = new Set();
    this.endpoints = { source: null, target: null };
    this.pathEdges = { shortest: new Set(), strongest: new Set(), sandbox: new Set() };
    this.pathNodes = new Set();
    this.expanded = new Set();
    this.mode = "both";       // which paths are emphasised: both | shortest | strongest | none
    this.focus = null;        // {nodes:Set, edges:Set} transient highlight (timeline step, editor selection)
    this.network = new vis.Network(container, { nodes: this.nodesDS, edges: this.edgesDS }, {
      nodes: { shape: "dot", size: 10, borderWidth: 1.5,
               font: { size: 13, color: css("--text"), strokeWidth: 3, strokeColor: css("--bg") } },
      edges: { arrows: { to: { enabled: true, scaleFactor: 0.45 } }, width: 1.2,
               color: { color: this.colors.edge, highlight: this.colors.select, hover: this.colors.select },
               font: { size: 10, color: css("--muted"), strokeWidth: 3, strokeColor: css("--bg"), align: "middle" },
               smooth: { type: "continuous" }, selectionWidth: 2 },
      physics: { solver: "forceAtlas2Based", forceAtlas2Based: { gravitationalConstant: -70, springLength: 120, avoidOverlap: 0.4 },
                 stabilization: { enabled: false }, maxVelocity: 30, minVelocity: 0.75 },
      interaction: { hover: true, tooltipDelay: 120, multiselect: true },
      layout: { randomSeed: 7 },
    });
    this.network.on("click", (params) => onSelect?.({ nodes: params.nodes, edges: params.nodes.length ? [] : params.edges }));
  }

  reset() {
    this.nodesDS.clear(); this.edgesDS.clear();
    this.nodes.clear(); this.edges.clear(); this.hidden.clear(); this.expanded.clear();
    this.pathEdges = { shortest: new Set(), strongest: new Set(), sandbox: new Set() };
    this.pathNodes = new Set(); this.focus = null; this.endpoints = { source: null, target: null };
  }

  setEndpoints(source, target) {
    this.endpoints = { source, target };
    // Start the two people apart so the graph grows from both ends.
    // Pinned horizontally (free vertically) so the graph grows between them and they never swap sides.
    for (const [id, x] of [[source, -360], [target, 360]]) {
      if (this.nodesDS.get(id)) this.nodesDS.update({ id, x, y: 0, fixed: { x: true, y: false }, ...this._nodeStyle(this.nodes.get(id)) });
    }
  }

  _role(n) {
    if (n.id === this.endpoints.source) return "source";
    if (n.id === this.endpoints.target) return "target";
    return n.kind in this.colors ? n.kind : "other";
  }

  _nodeStyle(n) {
    const role = this._role(n), big = role === "source" || role === "target";
    const c = this.colors[role];
    const lit = !this.focus || this.focus.nodes.has(n.id) || this.pathNodes.has(n.id) || big;
    const important = big || this.pathNodes.has(n.id) || this.expanded.has(n.id) || this.focus?.nodes.has(n.id);
    const showLabel = important || this.nodes.size <= this.labelThreshold;
    return {
      label: showLabel ? (n.label_en || n.label_es || n.id) : "",
      color: { background: c, border: big ? css("--text") : "#ffffff", highlight: { background: c, border: this.colors.select },
               hover: { background: c, border: this.colors.select } },
      shape: n.kind === "person" || big ? "dot" : "square",
      size: big ? 24 : n.kind === "person" ? 10 : 8 + Math.min(9, Math.log1p(n.degree || 0)),
      font: { size: big ? 17 : 13, bold: big ? { color: css("--text") } : undefined },
      opacity: lit ? 1 : 0.25,
    };
  }

  _edgeStyle(e) {
    const { shortest, strongest } = this.pathEdges;
    const showS = this.mode === "both" || this.mode === "shortest";
    const showW = this.mode === "both" || this.mode === "strongest";
    let color = this.colors.edge, width = 1.2, dashes = false;
    if (e.origin === "sandbox") { color = this.colors.sandbox; dashes = [6, 4]; width = 2; }
    if (showW && strongest.has(e.id)) { color = this.colors.strongest; width = 4; }
    if (showS && shortest.has(e.id)) { color = this.colors.shortest; width = strongest.has(e.id) && showW ? 5 : 4; }
    // A sandbox edge never takes a path colour: it stays pink and dashed so it can't pass for Wikidata.
    if (e.origin === "sandbox") color = this.colors.sandbox;
    if (this.focus?.edges.has(e.id)) { color = this.colors.select; width = Math.max(width, 3.5); }
    const lit = (showS && shortest.has(e.id)) || (showW && strongest.has(e.id)) || this.focus?.edges.has(e.id) || e.origin === "sandbox";
    const dimmed = (this.focus || (this.mode !== "none" && (shortest.size || strongest.size))) && !lit;
    return { color: { color: dimmed ? this.colors.dim : color, highlight: this.colors.select, hover: this.colors.select },
             width, dashes, label: e.origin === "sandbox" ? `${e.label} (sandbox)` : e.label || e.predicate,
             font: { color: dimmed ? "rgba(0,0,0,0)" : css("--muted") } };
  }

  addNodes(list) {
    const add = [];
    for (const n of list) {
      if (this.nodes.has(n.id)) continue;
      this.nodes.set(n.id, n);
      add.push({ id: n.id,
                 title: `${n.label_en || n.label_es || n.id} (${n.id})\n${n.kind} · ${n.degree} links in the snapshot`,
                 ...this._nodeStyle(n) });
    }
    if (add.length) {
      const crossed = this.nodes.size > this.labelThreshold && this.nodes.size - add.length <= this.labelThreshold;
      this.nodesDS.add(add);
      if (crossed) this.nodesDS.update([...this.nodes.values()].map((n) => ({ id: n.id, label: this._nodeStyle(n).label })));
    }
    return add.length;
  }

  addEdges(list, { fresh = false } = {}) {
    const add = [];
    for (const e of list) {
      if (this.edges.has(e.id)) continue;
      this.edges.set(e.id, e);
      const style = this._edgeStyle(e);
      if (fresh && e.origin !== "sandbox") { style.color = { ...style.color, color: this.colors.fresh }; style.width = 2.5; }
      add.push({ id: e.id, from: e.subject, to: e.object, title: `${e.label || e.predicate} (${e.predicate})${e.origin === "sandbox" ? " · SANDBOX, not Wikidata" : ""}`, ...style });
    }
    if (add.length) {
      this.edgesDS.add(add);
      if (fresh) setTimeout(() => this.restyleEdges(add.map((a) => a.id)), 1500);
    }
    return add.length;
  }

  removeEdges(ids) {
    for (const id of ids) this.edges.delete(id);
    this.edgesDS.remove(ids);
  }

  /**
   * Step back: keep only these nodes/edges (removing what later steps added) and reset the expanded
   * set. Remaining nodes keep their positions; the network itself is never rebuilt.
   */
  retainOnly(nodeIds, edgeIds, expanded) {
    const dropEdges = [...this.edges.keys()].filter((id) => !edgeIds.has(id) && this.edges.get(id).origin !== "sandbox");
    const dropNodes = [...this.nodes.keys()].filter((id) => !nodeIds.has(id));
    this.removeEdges(dropEdges);
    for (const id of dropNodes) { this.nodes.delete(id); this.hidden.delete(id); }
    this.nodesDS.remove(dropNodes);
    this.expanded = new Set(expanded);
    this.focus = null;
    this.restyleAll();
  }

  markExpanded(qid) {
    this.expanded.add(qid);
    const n = this.nodes.get(qid);
    if (n) this.nodesDS.update({ id: qid, label: this._nodeStyle(n).label });
  }

  setPaths(comparison, sandboxComparison = null) {
    const ids = (o) => new Set(o?.result ? o.result.hops.map((h) => h.edge_id) : []);
    const cmp = sandboxComparison || comparison;
    this.pathEdges = { shortest: ids(cmp?.shortest), strongest: ids(cmp?.strongest), sandbox: new Set() };
    this.pathNodes = new Set();
    for (const r of [cmp?.shortest?.result, cmp?.strongest?.result]) if (r) r.nodes.forEach((n) => this.pathNodes.add(n.qid));
    this.restyleAll();
  }

  setMode(mode) { this.mode = mode; this.restyleAll(); }

  setFocus(focus) {
    const nodes = new Set(focus?.nodes || []), edges = new Set(focus?.edges || []);  // accepts Sets or arrays
    this.focus = nodes.size || edges.size ? { nodes, edges } : null;
    if (this.focus) for (const id of this.focus.edges) {
      const e = this.edges.get(id); if (e) { this.focus.nodes.add(e.subject); this.focus.nodes.add(e.object); }
    }
    this.restyleAll();
  }

  restyleEdges(ids) {
    this.edgesDS.update(ids.filter((id) => this.edges.has(id)).map((id) => ({ id, ...this._edgeStyle(this.edges.get(id)) })));
  }

  restyleAll() {
    this.nodesDS.update([...this.nodes.values()].map((n) => ({ id: n.id, ...this._nodeStyle(n) })));
    this.restyleEdges([...this.edges.keys()]);
    this.applyCap();
  }

  /** Hide low-priority nodes beyond the cap. Returns the number of hidden nodes. */
  applyCap() {
    const all = [...this.nodes.values()];
    let hide = new Set();
    if (!this.showAll && all.length > this.maxVisible) {
      const linkCount = new Map();
      for (const e of this.edges.values()) {
        linkCount.set(e.subject, (linkCount.get(e.subject) || 0) + 1);
        linkCount.set(e.object, (linkCount.get(e.object) || 0) + 1);
      }
      const keep = (n) => n.id === this.endpoints.source || n.id === this.endpoints.target || this.pathNodes.has(n.id)
        || this.expanded.has(n.id) || this.focus?.nodes.has(n.id)
        || [...this.edges.values()].some((e) => e.origin === "sandbox" && (e.subject === n.id || e.object === n.id));
      const ranked = all.map((n) => ({ n, rank: keep(n) ? 0 : (linkCount.get(n.id) || 0) >= 2 ? 1 : 2, order: n.order ?? 0 }))
        .sort((a, b) => a.rank - b.rank || a.order - b.order);
      hide = new Set(ranked.slice(this.maxVisible).filter((r) => r.rank > 0).map((r) => r.n.id));
    }
    const changes = [];
    for (const n of all) {
      const was = this.hidden.has(n.id), now = hide.has(n.id);
      if (was !== now) changes.push({ id: n.id, hidden: now });
    }
    if (changes.length) this.nodesDS.update(changes);
    this.hidden = hide;
    return hide.size;
  }

  setShowAll(on) { this.showAll = on; this.applyCap(); }

  visibleEdgeIds() {
    return [...this.edges.values()].filter((e) => !this.hidden.has(e.subject) && !this.hidden.has(e.object)).map((e) => e.id);
  }

  select({ nodes = [], edges = [] }) {
    this.network.setSelection({ nodes: nodes.filter((id) => this.nodesDS.get(id)), edges: edges.filter((id) => this.edgesDS.get(id)) },
                              { unselectAll: true, highlightEdges: false });
  }

  /**
   * Settle the layout with a bounded number of physics iterations, then fit. Big graphs never fully
   * stop moving on their own, so a plain timeout would fit a graph that keeps drifting off-screen.
   */
  fitWhenSettled(nodeIds, iterations = 250) {
    let done = false;
    const go = () => { if (!done) { done = true; this.fitTo(nodeIds); } };
    this.network.once("stabilizationIterationsDone", go);
    this.network.stabilize(iterations);
    setTimeout(go, 4000);  // safety net
  }

  fitTo(nodeIds) {
    const ids = (nodeIds ? [...nodeIds] : []).filter((id) => this.nodesDS.get(id) && !this.hidden.has(id));
    // vis-network throws "Nodes has to be an array of ids" for `nodes: undefined`: omit the key to fit all.
    const options = { animation: { duration: 500, easingFunction: "easeInOutQuad" } };
    if (ids.length) options.nodes = ids;
    this.network.fit(options);
  }
}
