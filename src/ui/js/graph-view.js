// vis-network adapter. ONE vis.Network for the page's lifetime: events only add to / update its
// DataSets, so positions, selection and camera survive every update (plan §8).
//
// Visible-node caps. While stepping, every entity in focus is drawn and only `maxGreyed` of the
// greyed-out rest stay (connectors, then the latest arrivals). Without a story, beyond `maxVisible`
// the least important nodes are hidden (endpoints, path nodes and expanded nodes always stay).
// Hidden nodes and their statements also leave the physics simulation. "Show all" lifts both caps.

import { MOTION, REDUCED, easeInOut, easeOutBack } from "./motion.js";
import { drawEntity, mix, shapePath } from "./glyphs.js";

const FONT = '"Nunito Sans", -apple-system, "Segoe UI", system-ui, sans-serif';
const clamp01 = (t) => Math.min(1, Math.max(0, t));

/** compute_paths ping: each edge's ping leaves up to this fraction of a hop late, at random. */
const PING_JITTER = 0.3;
/** compute_paths ping: an edge's colour fades back to normal over this many ms after its ping arrives… */
const PING_FADE = 600;
/** …and the branch rests this long in its normal colours before its next ping leaves. */
const PING_REST = 500;
/** get_search_status: the one radar sweep before the ranking shows (ms), then each "#k" pill pops in
 *  RANK_STAGGER ms after the previous one, over RANK_POP ms. */
const RANK_SWEEP = 1800, RANK_STAGGER = 140, RANK_POP = 380;

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
/** "r,g,b" of a #rrggbb colour (for rgba() overlays); a neutral grey when it can't be parsed. */
const hexRgb = (hex) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec((hex || "").trim());
  return m ? [1, 2, 3].map((i) => parseInt(m[i], 16)).join(",") : "128,128,128";
};

export class GraphView {
  /** A step's new nodes are all labelled up to STORY_LABELS_ALL; beyond that only the top STORY_LABELS. */
  static STORY_LABELS_ALL = 30;
  static STORY_LABELS = 14;
  /** In a dense story, only the latest NAMED_RECENT expanded nodes keep their names (as in the card's trail line). */
  static NAMED_RECENT = 7;
  /** The two people stand out from everyone else; on the first step (only the two of them) they are larger still. */
  static ENDPOINT_SIZE = 34;
  static ENDPOINT_SPOT_SIZE = 36;
  static ENDPOINT_INTRO_SIZE = 60;

  /**
   * An entity's size (not one of the two people). While stepping, entities in focus (the trail, what
   * this step added, the planned branches, a clicked selection) are larger than the greyed-out rest;
   * without a story every entity has the plain size. Organizations, works and places grow a little
   * with their degree.
   */
  static entitySize(n, focus = null) {
    const grow = (k, cap) => Math.min(cap, k * Math.log1p(n.degree || 0));
    if (focus === true) return n.kind === "person" ? 19 : 15 + grow(0.9, 6);
    if (focus === false) return n.kind === "person" ? 11 : 9 + grow(0.6, 4);
    return n.kind === "person" ? 13 : 11 + grow(0.9, 7);
  }
  constructor(container, { maxVisible = 250, maxGreyed = 40, onSelect, images = null } = {}) {
    this.maxVisible = maxVisible;
    this.maxGreyed = maxGreyed;
    this.offEdges = new Set();  // edges taken out of the physics because an end is hidden
    this.images = images;     // ImageStore (images.js): entity pictures, drawn when loaded, else the pictogram
    this.container = container;
    this.labelZoom = 1;       // story labels are scaled by 1/zoom so they stay readable on screen
    this.fading = new Map();  // node id -> fade-in start time (new nodes and their edges fade in)
    this.rising = new Map();  // node id -> start time: a greyed node lighting up as a planned branch
    this.growing = [];        // branch tweens: [{id, origin, dx, dy, start}]
    this.fadeMs = MOTION.fade;
    this.growMs = MOTION.grow;
    this.labelThreshold = 60;  // above this many nodes, only important nodes keep a text label
    this.showAll = false;
    this.colors = {
      source: css("--c-source"), target: css("--c-target"), person: css("--c-person"),
      organization: css("--c-org"), position: css("--c-position"), other: css("--c-other"),
      work: css("--c-work"), place: css("--c-place"),
      edge: css("--c-edge"), dim: css("--c-dim"), shortest: css("--c-shortest"), strongest: css("--c-strongest"),
      fresh: css("--c-fresh"), freshSoft: css("--c-fresh-soft"), sandbox: css("--c-sandbox"), select: css("--c-select"),
      land: css("--bg"), surface: css("--surface"), text: css("--text"), muted: css("--muted"),
      road: css("--road"), roadMid: css("--road-mid"), roadDim: css("--road-dim"), rim: css("--highway-rim"),
    };
    // Per-node drawing facts the custom renderer needs beyond vis's own style (kind, label font, order
    // tag, last drawn radius), refreshed by _nodeStyle; pop-in and ripple start times.
    this.look = new Map();
    this.sizes = new Map();   // node id -> {from, to, start}: size changes glide (MOTION.resize) instead of jumping
    this.pop = new Map();
    this.ripples = new Map();
    this.pairs = new Map();   // "a|b" -> Set of edge ids: parallel statements get their own gentle curve
    this.nodesDS = new vis.DataSet();
    this.edgesDS = new vis.DataSet();
    this.nodes = new Map();   // id -> payload
    this.edges = new Map();   // id -> payload
    this.hidden = new Set();
    this.endpoints = { source: null, target: null };
    this.pathEdges = { shortest: new Set(), strongest: new Set(), sandbox: new Set() };
    this.pathNodes = new Set();
    this.prevPath = new Set();  // edges of the path a stronger one just replaced (that step only)
    this.targets = new Map();   // node id -> where straightenPath is gliding it (the camera frames these)
    this.expanded = new Set();
    this.mode = "strongest";  // which paths are emphasised: strongest | none (the demo shows the strongest path only)
    this.focus = null;        // {nodes:Set, edges:Set} transient highlight (timeline step, editor selection)
    // Step-through "story": the exploration trail (endpoints, expanded nodes in order, paths) stays in
    // colour, the current step's new nodes/edges are green, and everything else is greyed out.
    this.story = null;        // {current:Set, currentEdges:Set, order:Map qid->n, active:Set}
    // Flat, toy-like look (docs/visual-design.md): entities are drawn by _renderNode (shape + pictogram
    // + label), edges are thick straight "roads" with round joins and no arrowheads (the direction is
    // in the relation label, the tooltip and the Turtle).
    this.network = new vis.Network(container, { nodes: this.nodesDS, edges: this.edgesDS }, {
      nodes: { shape: "custom", ctxRenderer: (args) => this._renderNode(args), size: 12, borderWidth: 1 },
      edges: { arrows: { to: { enabled: false } }, width: 4,
               color: { color: this.colors.roadMid, highlight: this.colors.select, hover: this.colors.select },
               font: { face: FONT, size: 11, color: this.colors.muted, strokeWidth: 4, strokeColor: this.colors.land, align: "middle" },
               smooth: false, selectionWidth: 0, hoverWidth: 0,
               // a selected road gets a cream casing so it reads apart from the asphalt trail
               chosen: { edge: (v, id, selected) => { if (selected) { v.background = true; v.backgroundColor = this.colors.land; v.backgroundSize = v.width + 8; } } } },
      physics: { solver: "forceAtlas2Based", forceAtlas2Based: { gravitationalConstant: -70, springLength: 150, avoidOverlap: 0.4, damping: 0.6 },
                 // Calm physics: new nodes drift out from their parent instead of being flung ("popcorn").
                 stabilization: { enabled: false }, maxVelocity: 5, minVelocity: 0.5 },
      interaction: { hover: true, tooltipDelay: 120, multiselect: true },
      layout: { randomSeed: 7 },
    });
    this.network.on("click", (params) => onSelect?.({ nodes: params.nodes, edges: params.nodes.length ? [] : params.edges }));
    // The presenter's own mouse always wins over the step camera.
    this.network.on("dragStart", () => this.stopCamera());
    this.network.on("zoom", () => this.stopCamera());
    // Round caps and joins for every road (vis only sets them for dashed lines; save/restore keeps ours).
    this.network.on("beforeDrawing", (ctx) => { ctx.lineCap = "round"; ctx.lineJoin = "round"; this._drawSweep(ctx); });
    this.network.on("afterDrawing", (ctx) => { this._drawPathRoads(ctx); this._drawPave(ctx); this._drawPing(ctx); this._drawRanks(ctx); });  // overlays
  }

  reset() {
    this.stopCamera();
    if (this._fxRaf) cancelAnimationFrame(this._fxRaf);
    if (this._rankRaf) cancelAnimationFrame(this._rankRaf);
    this._fxRaf = null; this._ping = null; this._ranks = null; this._rankRaf = null; this._fxKeep = null;
    if (this._fadeRaf) cancelAnimationFrame(this._fadeRaf);
    if (this._growRaf) cancelAnimationFrame(this._growRaf);
    this._fadeRaf = null; this._growRaf = null; this.fading.clear(); this.rising.clear(); this.growing = [];
    this.nodesDS.clear(); this.edgesDS.clear();
    this.nodes.clear(); this.edges.clear(); this.hidden.clear(); this.expanded.clear();
    this.pathEdges = { shortest: new Set(), strongest: new Set(), sandbox: new Set() };
    if (this._straightRaf) cancelAnimationFrame(this._straightRaf);
    this._straightRaf = null; this.targets = new Map();
    this._stopPave(); this._sweep = null;
    this.look.clear(); this.pop.clear(); this.ripples.clear(); this.pairs.clear();
    this.pathNodes = new Set(); this.prevPath = new Set(); this.focus = null; this.story = null; this.labelZoom = 1; this.endpoints = { source: null, target: null };
    this.sizes.clear(); this.offEdges.clear();
  }

  /**
   * Step-through view. `order` lists expanded QIDs in exploration order (they get ① ② ③ badges);
   * `current`/`currentEdges` are what this step added; `active` is the node this step expanded.
   */
  setStory(story) {
    const before = this.story;
    this.story = story ? { current: new Set(story.current || []), currentEdges: new Set(story.currentEdges || []),
                           active: new Set(story.active || []), order: new Map((story.order || []).map((q, i) => [q, i + 1])),
                           planned: new Set(story.planned || []),
                           // Spotlight (a path moment, the result): only the path and the two people stay
                           // in full; everything else recedes to faint roads and plain pale dots, unnamed.
                           spotlight: !!story.spotlight,
                           // The first step: only the two people are on screen, shown large.
                           intro: !!story.intro } : null;
    this._fxKeep = null;  // what the previous step's tool effect needed on screen
    // A big expansion (100 new nodes) cannot label everything legibly: label its best-connected new
    // nodes, people first, so the audience can still read who arrived (the rest stay unlabelled dots).
    // On a path moment (`calm`) only the path and the trail keep labels: the path is the story there.
    if (this.story) this.story.named = story.calm ? new Set() : this._namedSubset(this.story.current, GraphView.STORY_LABELS);
    // Planned branches (the nodes the agent just decided to expand) light up gradually.
    const now = performance.now();
    for (const id of this.story?.planned || []) {
      const wasLit = before && (this._onTrail(id) || before.current.has(id) || before.planned?.has(id));
      if (!wasLit && !this._onTrail(id)) {
        this.rising.set(id, now);
        if (!REDUCED && !this.expanded.has(id)) this.ripples.set(id, now);  // one ring spreads out from it
      }
    }
    if (this.rising.size) this._runFade();
    this.restyleAll();
  }

  /** The nodes of `ids` worth a label: all of them when few, otherwise the top `k`, people first, by degree. */
  _namedSubset(ids, k) {
    const list = [...ids].map((id) => this.nodes.get(id)).filter(Boolean);
    if (list.length <= GraphView.STORY_LABELS_ALL) return new Set(list.map((n) => n.id));
    const person = (n) => (n.kind === "person" ? 0 : 1);
    list.sort((a, b) => person(a) - person(b) || (b.degree || 0) - (a.degree || 0) || (a.id < b.id ? -1 : 1));
    return new Set(list.slice(0, k).map((n) => n.id));
  }

  /** Lit in the story: on the trail, added by this step, or a planned branch of this turn. */
  _storyLit(id) {
    const st = this.story;
    return this._onTrail(id) || !!st?.current.has(id) || !!st?.planned.has(id);
  }

  _onTrail(id) {
    return id === this.endpoints.source || id === this.endpoints.target || this.pathNodes.has(id) || this.expanded.has(id);
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

  /** Spotlight is on (and not overridden by a clicked selection). */
  _spot() { return !!this.story?.spotlight && !this.focus; }

  _nodeStyle(n) {
    const role = this._role(n), big = role === "source" || role === "target";
    const c = this.colors[role];
    const st = this.story;
    if (this._spot()) return this._spotStyle(n, role, big, c);
    const current = !!st?.current.has(n.id), active = !!st?.active.has(n.id);
    const planned = !!st?.planned.has(n.id);
    const lit = this.focus ? this.focus.nodes.has(n.id) || this.pathNodes.has(n.id) || big
      : st ? this._storyLit(n.id) : true;
    const important = big || this.pathNodes.has(n.id) || this.expanded.has(n.id) || this.focus?.nodes.has(n.id)
      || planned || (current && st.named.has(n.id));
    const showLabel = important || (this.nodes.size <= this.labelThreshold && lit);
    const name = n.label_en || n.label_es || n.id;
    const rank = st?.order.get(n.id);
    // Dense graph: an older expanded node (not on the path, not part of this step) keeps only its
    // order badge, so 25 overlapping names don't bury the path. The last few expansions stay named.
    const badgeOnly = !!rank && this.nodes.size > this.labelThreshold && rank <= st.order.size - GraphView.NAMED_RECENT
      && !big && !this.pathNodes.has(n.id) && !active && !planned && !current && !this.focus?.nodes.has(n.id);
    // Dimmed entities are drawn in a solid pale colour (not see-through), so overlaps stay flat and clean.
    const fill = lit ? c : this.colors.dim;
    // A planned node (the agent's next call) sends out one ripple as it lights up, then keeps a dashed ring.
    const next = planned && !this.expanded.has(n.id);
    const sug = this._ranks?.get(n.id);
    if (sug) return this._rankedStyle(n, sug, name, rank, c);
    // While stepping the camera frames the whole trail, so trail labels are drawn larger to stay legible.
    const intro = big && !!st?.intro;
    const fontSize = intro ? 24 * this.labelZoom : st && lit ? (big ? 18 : active ? 17 : this._onTrail(n.id) || planned ? 15 : 12.5) * this.labelZoom : big ? 17 : active ? 16 : 13;
    this.look.set(n.id, { ...this.look.get(n.id), kind: big ? role : n.kind, dim: !lit, plain: false, halo: false, above: false, font: fontSize, bold: big || active,
                          caps: !big && n.kind !== "person", badge: showLabel && rank ? rank : null });
    return {
      label: !showLabel || badgeOnly ? "" : name,
      color: { background: fill, border: active || next ? this.colors.fresh : fill,
               highlight: { background: c, border: this.colors.select }, hover: { background: c, border: this.colors.select } },
      borderWidth: active ? 4 : next ? 3 : 1,
      shapeProperties: { borderDashes: next ? [5, 4] : false },
      size: (intro ? GraphView.ENDPOINT_INTRO_SIZE : big ? GraphView.ENDPOINT_SIZE
        : GraphView.entitySize(n, st ? lit : null)) + (active ? 3 : 0),
      font: { size: fontSize },
      opacity: (lit ? 0.7 + 0.3 * this._rise(n.id) : 1) * this._fade(n.id),
    };
  }

  /**
   * Spotlight style: the path's entities and the two people are shown large and named (no order tags,
   * they are clutter here); everything else is a small plain pale dot without pictogram or name.
   */
  _spotStyle(n, role, big, c) {
    const on = big || this.pathNodes.has(n.id);
    const name = n.label_en || n.label_es || n.id;
    const z = Math.max(1, this.labelZoom);
    if (on) {
      const font = (big ? 19 : 15) * z;
      // names alternate below / above the line along the path, so neighbours never run into each other
      const i = big ? 0 : [...this.pathNodes].indexOf(n.id);
      this.look.set(n.id, { ...this.look.get(n.id), kind: big ? role : n.kind, dim: false, plain: false, font, bold: true,
                            caps: !big && n.kind !== "person", badge: null, halo: true, above: i % 2 === 1 });
      return { label: name, color: { background: c, border: c, highlight: { background: c, border: this.colors.select }, hover: { background: c, border: this.colors.select } },
               borderWidth: 1, shapeProperties: { borderDashes: false },
               size: big ? GraphView.ENDPOINT_SPOT_SIZE : n.kind === "person" ? 20 : 18, font: { size: font }, opacity: this._fade(n.id) };
    }
    this.look.set(n.id, { ...this.look.get(n.id), kind: n.kind, dim: true, plain: true, font: 12, bold: false, caps: false, badge: null, halo: false, above: false });
    return { label: "", color: { background: this.colors.dim, border: this.colors.dim, highlight: { background: c, border: this.colors.select }, hover: { background: c, border: this.colors.select } },
             borderWidth: 1, shapeProperties: { borderDashes: false },
             size: n.kind === "person" ? 6 : 5.5, font: { size: 12 }, opacity: 0.7 * this._fade(n.id) };
  }

  /**
   * vis-network custom shape: a flat entity (circle for people, rounded square otherwise) with its
   * pictogram, a darker toy "lip", and a ring for state (active, planned, ranked, selected). The
   * size pops in with a small overshoot when the node arrives; a planned node sends out one ripple.
   */
  _renderNode({ ctx, id, x, y, state, style, label }) {
    const look = this.look.get(id) || { kind: "other" };
    const now = performance.now();
    const p0 = this.pop.get(id);
    const s = p0 === undefined || REDUCED ? 1 : 0.6 + 0.4 * easeOutBack(clamp01((now - p0) / MOTION.pop));
    const r = Math.max(2, this._glideSize(id, style.size || 12, now) * s);
    look.r = r;
    const sel = state.selected || state.hover;
    return {
      drawNode: () => {
        ctx.save();
        ctx.globalAlpha = style.opacity ?? 1;
        const q0 = this.ripples.get(id);
        if (q0 !== undefined) {
          const t = clamp01((now - q0) / MOTION.ripple);
          if (t < 1) {
            ctx.save();
            ctx.globalAlpha *= 0.55 * (1 - t);
            ctx.strokeStyle = this.colors.fresh;
            ctx.lineWidth = 3;
            shapePath(ctx, look.kind === "person" || look.kind === "source" || look.kind === "target", x, y, r + 4 + r * 1.1 * (1 - Math.pow(1 - t, 3)));
            ctx.stroke();
            ctx.restore();
          }
        }
        const ringOn = (style.borderWidth || 0) >= 2.5 || state.selected;
        // The entity's Wikidata image, once loaded; plain background dots stay dots.
        const photo = look.plain ? null : this.images?.get(id) || null;
        drawEntity(ctx, {
          kind: look.kind, x, y, r, fill: style.color, photo, photoGrey: !!look.dim,
          glyph: look.plain ? null : look.dim ? mix(style.color, "#ffffff", 0.5) : "#ffffff",
          lip: !look.dim,
          ring: ringOn ? (state.selected ? this.colors.select : style.borderColor) : sel ? this.colors.select : null,
          ringWidth: ringOn && !state.selected ? Math.min(4, style.borderWidth * 0.85) : 2,
          ringGap: 3,
          ringDash: Array.isArray(style.borderDashes) ? style.borderDashes : null,
        });
        ctx.restore();
      },
      drawExternalLabel: () => this._drawLabel(ctx, look, x, y, r, label, style.opacity ?? 1),
      nodeDimensions: { width: 2 * r, height: 2 * r },
    };
  }

  /**
   * The size to draw now: when a node's target size changes (the first step's large people shrinking,
   * an entity coming into or leaving focus), it glides there over MOTION.resize with the UI's
   * ease-in-out, starting from wherever it is, so a change mid-glide never jumps.
   */
  _glideSize(id, target, now) {
    if (REDUCED) return target;
    const t = this.sizes.get(id);
    if (!t) { this.sizes.set(id, { from: target, to: target, start: -Infinity }); return target; }
    if (t.to !== target) {
      t.from = this._sizeAt(t, now); t.to = target; t.start = now;
      this._runResize();
    }
    return this._sizeAt(t, now);
  }

  _sizeAt(t, now) {
    const k = clamp01((now - t.start) / MOTION.resize);
    return t.from + (t.to - t.from) * easeInOut(k);
  }

  /** Keep frames coming while any size glide runs (the renderer reads the glide on every draw). */
  _runResize() {
    if (this._resizeRaf) return;
    const tick = () => {
      const now = performance.now();
      this.network?.redraw();
      let busy = false;
      for (const t of this.sizes.values()) if (now - t.start < MOTION.resize) { busy = true; break; }
      this._resizeRaf = busy ? requestAnimationFrame(tick) : null;
    };
    this._resizeRaf = requestAnimationFrame(tick);
  }

  /** The entity's name under it (map-label style for non-people), and its exploration-order tag. */
  _drawLabel(ctx, look, x, y, r, label, opacity) {
    if (!label && !look.badge) return;
    const size = look.font || 13;
    ctx.globalAlpha = opacity;
    ctx.lineJoin = "round";
    if (label) {
      const fs = look.caps ? size * 0.82 : size;
      const text = look.caps ? label.toUpperCase() : label;
      ctx.font = `${look.bold ? 800 : 700} ${fs}px ${FONT}`;
      if ("letterSpacing" in ctx) ctx.letterSpacing = look.caps ? `${(fs * 0.07).toFixed(2)}px` : "0px";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const ty = look.above ? y - r - 6 - fs
        : y + r + 5 + (look.dim ? 0 : Math.max(1.5, r * 0.16));
      ctx.strokeStyle = this.colors.land;
      ctx.lineWidth = Math.max(3, fs * (look.halo ? 0.5 : 0.34));  // spotlight names get a wider halo
      ctx.strokeText(text, x, ty);
      ctx.fillStyle = look.dim ? this.colors.muted : this.colors.text;
      ctx.fillText(text, x, ty);
      if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
    }
    if (look.badge) {  // exploration order: a small rounded asphalt tag at the shape's top-right corner
      const fs = Math.max(9, size * 0.72), text = String(look.badge);
      ctx.font = `800 ${fs}px ${FONT}`;
      const h = fs * 1.45, w = Math.max(h, ctx.measureText(text).width + fs * 0.9);
      const cx = x + r * 0.78 + w * 0.28, cy = y - r * 0.78 - h * 0.15;
      ctx.fillStyle = this.colors.road;
      ctx.beginPath();
      ctx.roundRect(cx - w / 2, cy - h / 2, w, h, h * 0.42);
      ctx.fill();
      ctx.strokeStyle = this.colors.land;
      ctx.lineWidth = Math.max(1.5, fs * 0.16);
      ctx.stroke();
      ctx.fillStyle = this.colors.land;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, cx, cy + fs * 0.05);
    }
  }

  _edgeStyle(e) {
    const { shortest, strongest } = this.pathEdges;
    const showS = this.mode === "both" || this.mode === "shortest";
    const showW = this.mode === "both" || this.mode === "strongest";
    if (this._spot()) {
      // Spotlight: the path (drawn on top by _drawPathRoads) and the replaced path (dashed) only;
      // every other road is a faint thin line without a name. Sandbox roads keep their pink dashes.
      const fade = Math.min(this._fade(e.subject), this._fade(e.object));
      const hidden = { color: "rgba(0,0,0,0)", strokeWidth: 0 };
      const label = e.origin === "sandbox" ? `${e.label} (sandbox)` : e.label || e.predicate;
      if ((showW && strongest.has(e.id)) || (showS && shortest.has(e.id)))
        return { color: { color: this.colors.strongest, highlight: this.colors.select, hover: this.colors.select, opacity: fade }, width: 10, dashes: false, label, font: hidden };
      if (e.origin === "sandbox")
        return { color: { color: this.colors.sandbox, highlight: this.colors.select, hover: this.colors.select, opacity: fade }, width: 4, dashes: [9, 7], label, font: hidden };
      if (this.prevPath.has(e.id))
        return { color: { color: this.colors.strongest, highlight: this.colors.select, hover: this.colors.select, opacity: 0.7 * fade }, width: 4, dashes: [7, 9], label, font: hidden };
      return { color: { color: this.colors.roadDim, highlight: this.colors.select, hover: this.colors.select, opacity: 0.55 * fade }, width: 2, dashes: false, label, font: hidden };
    }
    // Roads: normal = mid, trail = asphalt, new/next = mint, path = a wide gold "highway" on an
    // asphalt rim, unexplored/dimmed = pale and thin. Sandbox stays pink and dashed.
    let color = this.colors.roadMid, width = 4, dashes = false, rim = false;
    if (e.origin === "sandbox") { color = this.colors.sandbox; dashes = [9, 7]; width = 4; }
    if (showW && strongest.has(e.id)) { color = this.colors.strongest; width = 10; rim = true; }
    if (showS && shortest.has(e.id)) { color = this.colors.shortest; width = 10; rim = true; }
    // A sandbox edge never takes a path colour: it stays pink and dashed so it can't pass for Wikidata.
    if (e.origin === "sandbox") { color = this.colors.sandbox; rim = false; }
    const onPath = (showS && shortest.has(e.id)) || (showW && strongest.has(e.id));
    const replaced = !onPath && this.prevPath.has(e.id) && e.origin !== "sandbox";
    if (replaced) { color = this.colors.strongest; width = 4; dashes = [7, 9]; }
    const st = this.focus ? null : this.story;
    const current = !!st?.currentEdges.has(e.id);
    // a big step (dozens of new roads) gets thinner new roads, so the fan doesn't turn into a green blot
    if (current && !onPath && e.origin !== "sandbox") { color = this.colors.fresh; width = st.current.size > GraphView.STORY_LABELS_ALL ? 3.5 : 6; }
    if (this.focus?.edges.has(e.id)) { color = this.colors.select; width = Math.max(width, 7); rim = false; }
    // Story: an edge between two trail nodes (e.g. Beyoncé → her sister, both expanded) is part of the
    // exploration's skeleton and stays; leaves fetched in earlier steps fade.
    // A planned branch (trail node → node the agent will expand next) shows too, but nothing beyond it.
    const branch = !!st && ((this._onTrail(e.subject) && st.planned.has(e.object)) || (st.planned.has(e.subject) && this._onTrail(e.object)));
    const trail = !!st && ((this._onTrail(e.subject) && this._onTrail(e.object)) || branch);
    if (branch && !onPath && !current) { color = this.colors.fresh; width = 6; }
    else if (trail && !onPath && !current && !replaced && e.origin !== "sandbox" && !this.focus?.edges.has(e.id)) { color = this.colors.road; width = 5; }
    const lit = onPath || replaced || this.focus?.edges.has(e.id) || e.origin === "sandbox" || current || trail;
    const dimmed = (this.focus || st || (this.mode !== "none" && (shortest.size || strongest.size))) && !lit;
    const fade = Math.min(this._fade(e.subject), this._fade(e.object)) * (branch ? 0.25 + 0.75 * Math.min(this._rise(e.subject), this._rise(e.object)) : 1)
      * (replaced ? 0.55 : 1);
    // A step that adds many nodes (e.g. 100 films) would print "cast member" on every edge: then only
    // path edges and a clicked edge keep their relation name.
    const crowded = !!st && st.current.size > GraphView.STORY_LABELS_ALL && !onPath && !this.focus?.edges.has(e.id);
    // A plain trail edge (two expanded nodes, not on the path) stays visible but unnamed: its label sat
    // mid-canvas on long pale edges and read as a word floating in empty space.
    const unnamed = !!st && !onPath && !replaced && !current && !branch && !this.focus?.edges.has(e.id) && e.origin !== "sandbox";
    return { color: { color: dimmed ? this.colors.roadDim : color, highlight: this.colors.select, hover: this.colors.select, opacity: fade },
             width: dimmed ? 3 : width, dashes,
             label: e.origin === "sandbox" ? `${e.label} (sandbox)` : e.label || e.predicate,
             // A hidden label also drops its outline (vis draws it even for transparent text, which
             // left gaps along the edges). Path roads and their names are drawn on top by _drawPathRoads.
             font: dimmed || crowded || unnamed || (rim && !this.focus?.edges.has(e.id)) ? { color: "rgba(0,0,0,0)", strokeWidth: 0 }
               : { color: this.colors.muted, strokeWidth: 4, size: 11 } };
  }

  /**
   * Add nodes. With `origin` (the node whose expansion produced them) they start packed in a small
   * sunflower spiral on top of it and fade in, so the physics grows them outward from their parent
   * instead of popping them up at random places.
   */
  addNodes(list, { origin = null } = {}) {
    const add = [];
    const at = origin && this.nodesDS.get(origin) ? this.network.getPosition(origin) : null;
    const fresh = list.filter((n) => !this.nodes.has(n.id));
    const rays = at ? this._fan(origin, at, fresh.length) : [];
    const now = performance.now(), gap = Math.min(70, 1000 / Math.max(1, fresh.length));
    let i = 0;
    for (const n of list) {
      if (this.nodes.has(n.id)) continue;
      this.nodes.set(n.id, n);
      const place = {};
      if (at) {
        // A branch: start on the parent, out of the physics, and grow along its ray (staggered).
        const { dx, dy } = rays[i], start = now + i * gap;
        Object.assign(place, { x: at.x + dx * 0.05, y: at.y + dy * 0.05, physics: false });
        this.growing.push({ id: n.id, origin, dx, dy, start });
        this.fading.set(n.id, start);
        this.pop.set(n.id, start);  // and pops to full size with a little overshoot
        i += 1;
      }
      add.push({ id: n.id,
                 title: `${n.label_en || n.label_es || n.id} (${n.id})\n${n.kind} · ${n.degree} links in the snapshot`,
                 ...place, ...this._nodeStyle(n) });
    }
    if (add.length) {
      const crossed = this.nodes.size > this.labelThreshold && this.nodes.size - add.length <= this.labelThreshold;
      this.nodesDS.add(add);
      this.images?.want(add.map((a) => a.id));
      if (crossed) this.nodesDS.update([...this.nodes.values()].map((n) => ({ id: n.id, label: this._nodeStyle(n).label })));
    }
    if (i) { this._runFade(); this._runGrow(); }
    return add.length;
  }

  /** A picture finished loading: redraw once for all that arrive within 120 ms. */
  pictureReady() {
    if (this._pictureTimer) return;
    this._pictureTimer = setTimeout(() => { this._pictureTimer = null; this.network?.redraw(); }, 120);
  }

  addEdges(list, { fresh = false } = {}) {
    const add = [];
    for (const e of list) {
      if (this.edges.has(e.id)) continue;
      this.edges.set(e.id, e);
      const style = this._edgeStyle(e);
      if (fresh && e.origin !== "sandbox") { style.color = { ...style.color, color: this.colors.fresh }; style.width = 6; }
      // Roads are straight; a second (third…) statement between the same two entities bends gently
      // to alternate sides so parallel predicates stay visible as separate roads.
      const key = e.subject < e.object ? `${e.subject}|${e.object}` : `${e.object}|${e.subject}`;
      if (!this.pairs.has(key)) this.pairs.set(key, new Set());
      const twins = this.pairs.get(key), k = twins.size;
      twins.add(e.id);
      if (k) style.smooth = { enabled: true, type: (k % 2) === (e.subject < e.object ? 1 : 0) ? "curvedCW" : "curvedCCW", roundness: 0.14 * Math.ceil(k / 2) };
      add.push({ id: e.id, from: e.subject, to: e.object, title: `${e.label || e.predicate} (${e.predicate})${e.origin === "sandbox" ? " · SANDBOX, not Wikidata" : ""}`, ...style });
    }
    if (add.length) {
      this.edgesDS.add(add);
      if (fresh) setTimeout(() => this.restyleEdges(add.map((a) => a.id)), MOTION.fresh);
    }
    return add.length;
  }

  /** 0..1 fade-in factor of a node (1 once it has fully appeared). */
  _fade(id) {
    const t0 = this.fading.get(id);
    if (t0 === undefined) return 1;
    const t = Math.min(1, Math.max(0, (performance.now() - t0) / this.fadeMs));
    return Math.max(0.02, t * t * (3 - 2 * t));  // smoothstep
  }

  /** 0..1 light-up factor of a planned branch node (1 when not rising). */
  _rise(id) {
    const t0 = this.rising.get(id);
    if (t0 === undefined) return 1;
    const t = Math.min(1, Math.max(0, (performance.now() - t0) / this.fadeMs));
    return t * t * (3 - 2 * t);
  }

  /**
   * Ray offsets for `n` children of a node at `at`: fanned away from the parent's existing
   * neighbours (so the tree keeps growing outward), a full circle when it has none yet.
   */
  _fan(origin, at, n) {
    if (!n) return [];
    let cx = 0, cy = 0, k = 0;
    for (const e of this.edges.values()) {
      const other = e.subject === origin ? e.object : e.object === origin ? e.subject : null;
      if (!other || !this.nodesDS.get(other)) continue;
      const p = this.network.getPosition(other);
      cx += p.x; cy += p.y; k += 1;
    }
    const full = !k || n > 16;
    const base = k ? Math.atan2(at.y - cy / k, at.x - cx / k) : 0;
    const span = full ? 2 * Math.PI : n <= 2 ? 0.5 * Math.PI : n <= 6 ? 0.9 * Math.PI : 1.4 * Math.PI;
    return Array.from({ length: n }, (_, i) => {
      const a = full ? base + (2 * Math.PI * i) / n : base - span / 2 + (span * (i + 0.5)) / n;
      const r = 150 + (n > 24 ? 50 * (i % 3) : 0);  // big bursts: three rings
      return { dx: r * Math.cos(a), dy: r * Math.sin(a) };
    });
  }

  /** Move growing branches out from their (possibly moving) parent; release them to the physics at the end. */
  _runGrow() {
    if (this._growRaf) return;
    const ease = (t) => 1 - Math.pow(1 - t, 3);
    const tick = () => {
      const now = performance.now(), done = [];
      this.growing = this.growing.filter((g) => {
        if (!this.nodes.has(g.id)) return false;
        if (!this.nodesDS.get(g.origin)) { done.push(g.id); return false; }
        const t = Math.min(1, Math.max(0, (now - g.start) / this.growMs)), e = 0.05 + 0.95 * ease(t);
        const p = this.network.getPosition(g.origin);
        this.network.moveNode(g.id, p.x + g.dx * e, p.y + g.dy * e);
        if (t >= 1) { done.push(g.id); return false; }
        return true;
      });
      // back to the physics, unless the cap hid the node meanwhile (hidden nodes are not simulated)
      if (done.length) this.nodesDS.update(done.map((id) => ({ id, physics: !this.hidden.has(id) })));
      this._growRaf = this.growing.length ? requestAnimationFrame(tick) : null;
    };
    this._growRaf = requestAnimationFrame(tick);
  }

  _runFade() {
    if (this._fadeRaf) return;
    const tick = () => {
      const now = performance.now();
      const moving = (id) => this.fading.has(id) || this.rising.has(id);
      const ids = [...new Set([...this.fading.keys(), ...this.rising.keys()])].filter((id) => this.nodes.has(id));
      this.nodesDS.update(ids.map((id) => {
        const st = this._nodeStyle(this.nodes.get(id));
        return this.rising.has(id) ? { id, opacity: st.opacity, size: st.size } : { id, opacity: st.opacity };
      }));
      const touching = [...this.edges.values()].filter((e) => moving(e.subject) || moving(e.object));
      this.edgesDS.update(touching.map((e) => { const st = this._edgeStyle(e); return { id: e.id, color: st.color, width: st.width }; }));
      for (const map of [this.fading, this.rising]) {
        for (const [id, t0] of map) if (now - t0 >= this.fadeMs || !this.nodes.has(id)) map.delete(id);
      }
      for (const [id, t0] of this.pop) if (now - t0 >= MOTION.pop) this.pop.delete(id);
      for (const [id, t0] of this.ripples) if (now - t0 >= MOTION.ripple || !this.nodes.has(id)) this.ripples.delete(id);
      // pops and ripples are drawn by the node renderer: keep frames coming while they run
      if (this.ripples.size || this.pop.size) this.network.redraw();
      this._fadeRaf = this.fading.size || this.rising.size || this.ripples.size || this.pop.size ? requestAnimationFrame(tick) : null;
    };
    this._fadeRaf = requestAnimationFrame(tick);
  }

  removeEdges(ids) {
    for (const id of ids) {
      const e = this.edges.get(id);
      if (e) this.pairs.get(e.subject < e.object ? `${e.subject}|${e.object}` : `${e.object}|${e.subject}`)?.delete(id);
      this.edges.delete(id);
      this.offEdges.delete(id);
    }
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
    for (const id of dropNodes) { this.nodes.delete(id); this.hidden.delete(id); this.look.delete(id); this.pop.delete(id); this.ripples.delete(id); this.sizes.delete(id); }
    this.nodesDS.remove(dropNodes);
    this.expanded = new Set(expanded);
    this.focus = null;
    this.restyleAll();
  }

  /**
   * An expanded node is a parent in the tree: it is pinned where it stands the moment it is expanded
   * (and made heavy), so its branches grow from a fixed point and the trail holds still. Only leaves
   * stay physics-driven, which also gives the step camera a stable target.
   */
  markExpanded(qid) {
    this.expanded.add(qid);
    const n = this.nodes.get(qid);
    if (n) this.nodesDS.update({ id: qid, label: this._nodeStyle(n).label, mass: 6, fixed: { x: true, y: true } });
  }

  /**
   * Pin nodes where they stand (path nodes once a path appears): leaves on a path would otherwise keep
   * drifting with the physics, out of the frame the step camera computed once (behind the step card).
   * Nodes still growing out of their parent are pinned when their tween has ended.
   */
  pinNodes(ids) {
    const pin = (list) => {
      const ok = list.filter((id) => this.nodesDS.get(id));
      if (ok.length) this.nodesDS.update(ok.map((id) => ({ id, fixed: { x: true, y: true } })));
    };
    const all = [...ids], growing = new Set(this.growing.map((g) => g.id));
    pin(all.filter((id) => !growing.has(id)));
    const later = all.filter((id) => growing.has(id));
    if (later.length) setTimeout(() => pin(later), this.growthRemaining() + 50);
  }

  /** Branch tweens still running: when the last one ends (ms from now), 0 if none. */
  growthRemaining() {
    if (!this.growing.length) return 0;
    return Math.max(0, Math.max(...this.growing.map((g) => g.start)) + this.growMs - performance.now());
  }

  setPaths(comparison, sandboxComparison = null) {
    this._stopPave();
    const ids = (o) => new Set(o?.result ? o.result.hops.map((h) => h.edge_id) : []);
    const cmp = sandboxComparison || comparison;
    this._hopLabels(cmp);
    // Demo choice: only the strongest path is shown (the backend still computes both).
    this.pathEdges = { shortest: new Set(), strongest: ids(cmp?.strongest), sandbox: new Set() };
    this.pathNodes = new Set(cmp?.strongest?.result ? cmp.strongest.result.nodes.map((n) => n.qid) : []);
    this.restyleAll();
  }

  /**
   * Like setPaths, but the new path is PAVED: a gold band runs continuously along each hop, one hop
   * every MOTION.pave ms, from the first person to the other (strongest path only). As a hop is
   * finished its real path style is committed. Used when a path first appears or improves.
   */
  revealPaths(comparison) {
    this._stopPave();
    const r = comparison?.strongest?.result;
    const st = r ? r.hops.map((h) => h.edge_id) : [], stN = r ? r.nodes.map((n) => n.qid) : [];
    const total = st.length;
    if (!total || REDUCED) { this.setPaths(comparison); return; }
    this._hopLabels(comparison);
    this.pathEdges = { shortest: new Set(), strongest: new Set(), sandbox: new Set() };
    this.pathNodes = new Set();
    this.restyleAll();
    this._pave = { edges: st, nodes: stN, t0: performance.now(), done: 0 };
    const tick = (now) => {
      const p = this._pave;
      if (!p) return;
      const k = Math.min(total, Math.floor((now - p.t0) / MOTION.pave));
      if (k > p.done) {
        p.done = k;
        this.pathEdges = { shortest: new Set(), strongest: new Set(st.slice(0, k)), sandbox: new Set() };
        this.pathNodes = new Set(stN.slice(0, k + 1));
        this.restyleAll();
      }
      this.network.redraw();
      if (k < total) this._paveRaf = requestAnimationFrame(tick);
      else { this._pave = null; this._paveRaf = null; }
    };
    this._paveRaf = requestAnimationFrame(tick);
  }

  /** Relation names of the path's hops in walking direction ("acted in", not the stored "cast member"), as on the card. */
  _hopLabels(cmp) {
    this.hopLabel = new Map();
    for (const h of cmp?.strongest?.result?.hops || []) if (h.label) this.hopLabel.set(h.edge_id, h.label);
  }

  /** Stop a path being paved (the next setPaths/revealPaths takes over). */
  _stopPave() {
    clearTimeout(this._traceTimer);
    if (this._paveRaf) cancelAnimationFrame(this._paveRaf);
    this._paveRaf = null; this._pave = null;
  }

  /** afterDrawing hook: the hop being paved, a gold band on its asphalt rim growing along the road. */
  _drawPave(ctx) {
    const p = this._pave;
    if (!p || p.done >= p.edges.length) return;
    const id = p.edges[p.done], near = p.nodes[p.done];
    if (!this.edges.has(id) || !this.nodesDS.get(near)) return;
    const frac = clamp01((performance.now() - p.t0) / MOTION.pave - p.done);
    if (frac <= 0) return;
    const e = easeInOut(frac), n = Math.max(2, Math.ceil(e * 18)), pts = [];
    for (let i = 0; i <= n; i++) pts.push(this._edgePoint(id, near, (e * i) / n));
    ctx.save();
    this._clipOutNodes(ctx);
    for (const [color, w] of [[this.colors.rim, 15], [this.colors.strongest, 10]]) {
      ctx.beginPath();
      pts.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
      ctx.strokeStyle = color;
      ctx.lineWidth = w;
      ctx.lineCap = "round";
      ctx.stroke();
    }
    ctx.restore();
  }

  /**
   * afterDrawing hook: the path's roads drawn again ON TOP of every other road (vis draws edges in
   * insertion order, so a step's many new roads buried the path), still under the entities, with
   * their relation names. vis's own copy of these edges keeps their hit area and tooltip.
   */
  _drawPathRoads(ctx) {
    const ids = [...this.pathEdges.strongest, ...this.pathEdges.shortest]
      .filter((id) => !this.focus?.edges.has(id) && this.edges.get(id)?.origin !== "sandbox");
    if (!ids.length) return;
    ctx.save();
    this._clipOutNodes(ctx);
    const lines = [];
    for (const id of ids) {
      const e = this.edges.get(id);
      if (!e || this.hidden.has(e.subject) || this.hidden.has(e.object) || !this.nodesDS.get(e.subject) || !this.nodesDS.get(e.object)) continue;
      const pts = [];
      for (let i = 0; i <= 16; i++) pts.push(this._edgePoint(id, e.subject, i / 16));
      lines.push({ e, pts, color: this.pathEdges.strongest.has(id) ? this.colors.strongest : this.colors.shortest });
    }
    for (const [key, w] of [["rim", 15], ["fill", 10]]) {
      for (const { pts, color } of lines) {
        ctx.beginPath();
        pts.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
        ctx.strokeStyle = key === "rim" ? this.colors.rim : color;
        ctx.lineWidth = w;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.stroke();
      }
    }
    ctx.restore();
    // Relation names as small signs ON the road (a cream pill with a gold outline), upright, sized like
    // the entity names (scaled against the camera zoom, so they stay readable when it zooms out).
    ctx.save();
    const z = Math.max(1, this.labelZoom), fs = 12.5 * z;
    ctx.font = `800 ${fs}px ${FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const { e, pts } of lines) {
      const a = pts[7], b = pts[9], m = pts[8];
      let ang = Math.atan2(b.y - a.y, b.x - a.x);
      if (ang > Math.PI / 2) ang -= Math.PI; else if (ang < -Math.PI / 2) ang += Math.PI;
      const text = this.hopLabel?.get(e.id) || e.label || e.predicate;
      const w = ctx.measureText(text).width + fs * 1.1, h = fs * 1.75;
      ctx.save();
      ctx.translate(m.x, m.y);
      ctx.rotate(ang);
      ctx.beginPath();
      ctx.roundRect(-w / 2, -h / 2, w, h, h / 2);
      ctx.fillStyle = this.colors.surface;
      ctx.fill();
      ctx.strokeStyle = this.colors.strongest;
      ctx.lineWidth = 2.5 * z;
      ctx.stroke();
      ctx.fillStyle = this.colors.text;
      ctx.fillText(text, 0, fs * 0.06);
      ctx.restore();
    }
    ctx.restore();
    // the path's entities keep their names above the path roads
    for (const id of this.pathNodes) {
      const bn = this.network.body.nodes[id], look = this.look.get(id);
      if (!bn || bn.options.hidden || !look?.r) continue;
      ctx.save();
      this._drawLabel(ctx, look, bn.x, bn.y, look.r, bn.options.label || "", bn.options.opacity ?? 1);
      ctx.restore();
    }
  }

  /** Clip drawing to everything outside the visible nodes, so overlays pass under them. */
  _clipOutNodes(ctx) {
    ctx.beginPath();
    ctx.rect(-1e7, -1e7, 2e7, 2e7);
    for (const bn of Object.values(this.network.body.nodes)) {
      if (bn.options.hidden || !this.nodes.has(bn.id)) continue;
      const rad = (this.look.get(bn.id)?.r || bn.options.size || 10) * 1.18 + 2;  // covers rounded-square corners
      ctx.moveTo(bn.x + rad, bn.y);
      ctx.arc(bn.x, bn.y, rad, 0, 2 * Math.PI);
    }
    ctx.clip("evenodd");
  }

  setMode(mode) { this.mode = mode; this.restyleAll(); }

  /** The path a stronger one just replaced: drawn faint and dashed for this step only ([] clears). */
  setPreviousPath(edgeIds) {
    this.prevPath = new Set(edgeIds || []);
  }

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

  /** Hide low-priority nodes beyond the caps. Returns the number of hidden nodes. */
  applyCap() {
    const all = [...this.nodes.values()];
    let hide = new Set();
    if (!this.showAll) {
      const linkCount = new Map(), sandbox = new Set();
      for (const e of this.edges.values()) {
        linkCount.set(e.subject, (linkCount.get(e.subject) || 0) + 1);
        linkCount.set(e.object, (linkCount.get(e.object) || 0) + 1);
        if (e.origin === "sandbox") { sandbox.add(e.subject); sandbox.add(e.object); }
      }
      const keep = (n) => n.id === this.endpoints.source || n.id === this.endpoints.target || this.pathNodes.has(n.id)
        || this.expanded.has(n.id) || this.focus?.nodes.has(n.id) || sandbox.has(n.id);
      const connector = (n) => ((linkCount.get(n.id) || 0) >= 2 ? 1 : 0);
      if (this.story) {
        // Stepping: what the step is about is always drawn; the greyed-out rest is only context, so
        // just a few stay: connectors first (they join parts of the story), then the latest arrivals.
        const lit = (n) => keep(n) || this._storyLit(n.id) || !!this._ranks?.has(n.id) || !!this._fxKeep?.has(n.id);
        const arrival = new Map([...this.nodes.keys()].map((id, i) => [id, i]));
        const grey = all.filter((n) => !lit(n))
          .sort((a, b) => connector(b) - connector(a) || arrival.get(b.id) - arrival.get(a.id));
        hide = new Set(grey.slice(this.maxGreyed).map((n) => n.id));
      } else if (all.length > this.maxVisible) {
        const ranked = all.map((n) => ({ n, rank: keep(n) ? 0 : connector(n) ? 1 : 2, order: n.order ?? 0 }))
          .sort((a, b) => a.rank - b.rank || a.order - b.order);
        hide = new Set(ranked.slice(this.maxVisible).filter((r) => r.rank > 0).map((r) => r.n.id));
      }
    }
    const growing = new Set(this.growing.map((g) => g.id));  // _runGrow hands these back to the physics itself
    const changes = [];
    for (const n of all) {
      const was = this.hidden.has(n.id), now = hide.has(n.id);
      if (was !== now) changes.push(growing.has(n.id) ? { id: n.id, hidden: now } : { id: n.id, hidden: now, physics: !now });
    }
    this.hidden = hide;
    if (changes.length) this.nodesDS.update(changes);
    // A statement with a hidden end leaves the physics too, or its spring would keep pulling the
    // visible end towards a node that no longer moves.
    const edgeChanges = [];
    for (const e of this.edges.values()) {
      const off = hide.has(e.subject) || hide.has(e.object);
      if (off !== this.offEdges.has(e.id)) {
        edgeChanges.push({ id: e.id, physics: !off });
        if (off) this.offEdges.add(e.id); else this.offEdges.delete(e.id);
      }
    }
    if (edgeChanges.length) this.edgesDS.update(edgeChanges);
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

  /**
   * Step-through camera: glide (ease-in-out) to frame `nodeIds` in the canvas area the step card
   * leaves free: `inset` is {top, left} in pixels (a number means a top inset). The target is computed ONCE, from where growing branches will END, so
   * the camera follows one smooth curve instead of chasing moving nodes. If the nodes are already
   * comfortably in view it does not move at all. Never calls network.stabilize() (calling it while
   * the simulation runs unbalances vis-network's render counter and freezes later view animations,
   * which then undo every mouse zoom/drag). A user drag or wheel zoom stops the glide at once.
   */
  frameStory(focus, inset = 0, duration = MOTION.camera) {
    this.stopCamera();
    const box = this._freeBox(inset);
    // focus: {center, around}. The view is centred on `center` (e.g. the parent whose branches grow)
    // and zoomed out just enough to keep `around` (where those branches will end) in frame.
    const center = [...(focus.center || [])], ids = [...new Set([...center, ...(focus.around || [])])];
    const target = this._frameTarget(ids, box, center);
    if (!target) return;
    const from = { scale: this.network.getScale(), pos: this.network.getViewPosition() };
    const settle = (scale) => {
      this.labelZoom = Math.min(4, Math.max(1, 1 / scale));
      this.nodesDS.update([...this.nodes.values()].map((n) => ({ id: n.id, font: this._nodeStyle(n).font })));
    };
    if (this._inView(ids, box, from.scale, target.scale, center)) { settle(from.scale); return; }
    settle(target.scale);
    duration = Math.max(duration, this.growthRemaining() + 350);
    const t0 = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - t0) / duration), e = easeInOut(t);
      this.network.moveTo({
        scale: from.scale * Math.pow(target.scale / from.scale, e),  // geometric: zoom feels uniform
        position: { x: from.pos.x + (target.position.x - from.pos.x) * e, y: from.pos.y + (target.position.y - from.pos.y) * e },
      });
      this._camRaf = t < 1 ? requestAnimationFrame(tick) : null;
    };
    this._camRaf = requestAnimationFrame(tick);
  }

  /**
   * Lay a path out the way it reads: its nodes glide (MOTION.camera, eased) to evenly spaced points on
   * the line between the two people, so "connected" LOOKS connected and screen distance stops
   * contradicting the hop count. `qids` runs from one person to the other; the people stay put (and
   * are pinned, so the line holds). `bow` bends the line into an arc (canvas units, perpendicular),
   * used when a stronger path replaces one already laid out straight. The nodes stay pinned there.
   */
  straightenPath(qids, { bow = 0, duration = MOTION.camera } = {}) {
    const ids = qids.filter((id) => this.nodesDS.get(id));
    if (ids.length < 3) return;
    let a = this.network.getPosition(ids[0]), b = this.network.getPosition(ids[ids.length - 1]);
    const n = ids.length - 1;
    // Every hop needs room for its relation sign and the names around it: when the people are too
    // close for that, they glide apart (about their midpoint) until each hop gets MIN_HOP units.
    const MIN_HOP = 190, ends = [];
    {
      const l0 = Math.hypot(b.x - a.x, b.y - a.y);
      if (l0 < n * MIN_HOP) {
        const ux = l0 ? (b.x - a.x) / l0 : 1, uy = l0 ? (b.y - a.y) / l0 : 0, mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, h = (n * MIN_HOP) / 2;
        const na = { x: mx - ux * h, y: my - uy * h }, nb = { x: mx + ux * h, y: my + uy * h };
        ends.push({ id: ids[0], from: a, to: na }, { id: ids[ids.length - 1], from: b, to: nb });
        a = na; b = nb;
      }
    }
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1, px = -dy / len, py = dx / len;
    const mids = ids.slice(1, -1).map((id, i) => {
      const k = (i + 1) / n, arc = bow * Math.sin(Math.PI * k);
      return { id, from: this._finalPosition(id), to: { x: a.x + dx * k + px * arc, y: a.y + dy * k + py * arc } };
    });
    const moves = [...mids, ...ends];
    // Entities that merely sit on the line would read as part of the path ("Paulo → ③ → UN"): they
    // glide off it, sideways, to CLEAR canvas units from the nearest segment.
    const CLEAR = 70, pts = [a, ...mids.map((m) => m.to), b], onPath = new Set(ids);
    for (const id of this.nodes.keys()) {
      if (onPath.has(id) || this.hidden.has(id) || !this.nodesDS.get(id)) continue;
      const q = this._finalPosition(id);
      let best = null;
      for (let i = 0; i + 1 < pts.length; i++) {
        const s = pts[i], e = pts[i + 1], vx = e.x - s.x, vy = e.y - s.y, l2 = vx * vx + vy * vy || 1;
        const u = Math.max(0, Math.min(1, ((q.x - s.x) * vx + (q.y - s.y) * vy) / l2));
        const cx = s.x + u * vx, cy = s.y + u * vy, d = Math.hypot(q.x - cx, q.y - cy);
        if (!best || d < best.d) best = { d, cx, cy, vx, vy };
      }
      if (!best || best.d >= CLEAR) continue;
      // away from the line, on the side the node already is (or "up" when it sits exactly on it)
      let nx = q.x - best.cx, ny = q.y - best.cy, nl = Math.hypot(nx, ny);
      if (nl < 1e-6) { const l = Math.hypot(best.vx, best.vy) || 1; nx = best.vy / l; ny = -best.vx / l; nl = 1; }
      moves.push({ id, from: q, to: { x: best.cx + (nx / nl) * CLEAR, y: best.cy + (ny / nl) * CLEAR }, aside: true });
    }
    // A node still growing out of its parent stops growing: it goes to its place on the path instead.
    const moving = new Set(moves.filter((m) => !m.aside).map((m) => m.id));
    this.growing = this.growing.filter((g) => !moves.some((m) => m.aside && m.id === g.id));
    this.growing = this.growing.filter((g) => !moving.has(g.id));
    this.targets = new Map(moves.map((m) => [m.id, m.to]));
    this.nodesDS.update([ids[0], ids[ids.length - 1], ...moving].map((id) => ({ id, fixed: { x: true, y: true } })));
    if (this._straightRaf) cancelAnimationFrame(this._straightRaf);
    const t0 = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - t0) / duration), e = easeInOut(t);
      for (const m of moves) {
        if (this.nodesDS.get(m.id)) this.network.moveNode(m.id, m.from.x + (m.to.x - m.from.x) * e, m.from.y + (m.to.y - m.from.y) * e);
      }
      if (t < 1) this._straightRaf = requestAnimationFrame(tick);
      else { this._straightRaf = null; this.targets = new Map(); }
    };
    this._straightRaf = requestAnimationFrame(tick);
  }

  /** Where a node will be once its branch has grown or its path glide ends (its current position otherwise). */
  _finalPosition(id) {
    const to = this.targets.get(id);
    if (to) return to;
    const g = this.growing.find((x) => x.id === id);
    if (g && this.nodesDS.get(g.origin)) {
      const p = this.network.getPosition(g.origin);
      return { x: p.x + g.dx, y: p.y + g.dy };
    }
    return this.network.getPosition(id);
  }

  /** True when every node is inside the free area and the zoom is not far from the ideal one. */
  _inView(ids, box, scale, idealScale, center = []) {
    if (scale < idealScale * 0.6 || scale > idealScale * 1.8) return false;
    const { top, left, width, height, bottom } = box;
    const midX = (left + width) / 2, freeW = width - left;
    const midY = (top + height - bottom) / 2, freeH = height - bottom - top;
    const offCentre = center.filter((id) => this.nodesDS.get(id)).some((id) => {
      const p = this.network.canvasToDOM(this._finalPosition(id));
      return Math.abs(p.x - midX) > freeW * 0.2 || Math.abs(p.y - midY) > freeH * 0.2;
    });
    if (offCentre) return false;
    const margin = 110;  // room for the label beside a node
    return ids.filter((id) => this.nodesDS.get(id) && !this.hidden.has(id)).every((id) => {
      const p = this.network.canvasToDOM(this._finalPosition(id));
      return p.x > left + margin && p.x < width - margin && p.y > top + 20 && p.y < height - 60;
    });
  }

  // ---- step effects (presentation only: they depict what a tool does, never its result) ----------

  stopFx() {
    const had = this._fxRaf || this._ranks;
    if (this._fxRaf) cancelAnimationFrame(this._fxRaf);
    if (this._rankRaf) cancelAnimationFrame(this._rankRaf);
    this._fxRaf = null; this._rankRaf = null;
    this._ping = null; this._ranks = null; this._sweep = null;
    if (!had) return;
    this.restyleAll();
    this.network.redraw();
  }

/**
   * Run an effect until stopFx() (the next step) or for `duration` ms. Frames are throttled to
   * ~25 fps: the effects restyle hundreds of edges per frame.
   */
  _runFx(duration, frame, minGap = 40, onDone = null) {
    this.stopFx();
    const t0 = performance.now();
    let last = -Infinity;
    const tick = (now) => {
      const ms = now - t0, t = Number.isFinite(duration) ? Math.min(1, ms / duration) : 0;
      if (now - last >= minGap) { last = now; frame(t, ms); }
      if (t < 1) this._fxRaf = requestAnimationFrame(tick);
      else { this._fxRaf = null; onDone?.(); this.restyleAll(); }
    };
    this._fxRaf = requestAnimationFrame(tick);
  }

  /**
   * Hops from `start` the way the backend searches the explored graph: hubs (non-person nodes over
   * their kind's cap) are never entered, the other person ends a route, at most `maxHops` deep.
   * With `through`, only those nodes are expanded (rank_frontier only knows expanded nodes' edges).
   */
  _searchHops(start, { isHub, maxHops }, through = null) {
    const adj = new Map();
    for (const e of this.edges.values()) {
      if (e.origin === "sandbox") continue;
      for (const [a, b] of [[e.subject, e.object], [e.object, e.subject]]) {
        if (!adj.has(a)) adj.set(a, []);
        adj.get(a).push(b);
      }
    }
    const { source, target } = this.endpoints, other = start === source ? target : source;
    const dist = new Map([[start, 0]]);
    let frontier = [start];
    while (frontier.length) {
      const next = [];
      for (const v of frontier) {
        if (dist.get(v) >= maxHops || (v !== start && v === other) || (through && !through.has(v))) continue;
        for (const w of adj.get(v) || []) {
          if (dist.has(w)) continue;
          if (w !== other && isHub(this.nodes.get(w))) continue;  // hubs are never on a route
          dist.set(w, dist.get(v) + 1);
          next.push(w);
        }
      }
      frontier = next;
    }
    return dist;
  }

  /** Angle (0..2π) of point p around node `root`'s current position. */
  _angle(pos, root, p) {
    const c = pos[root];
    return (Math.atan2(p.y - c.y, p.x - c.x) + 2 * Math.PI) % (2 * Math.PI);
  }

  /** Point at `frac` (0 = near end, 1 = far end) along an edge as vis draws it (curves included). */
  _edgePoint(id, near, frac) {
    const e = this.edges.get(id), body = this.network.body.edges[id];
    const pos = near === e.subject ? frac : 1 - frac;  // vis measures from the edge's `from` (subject)
    try {
      const p = body?.edgeType?.getPoint(pos);
      if (p && Number.isFinite(p.x)) return p;
    } catch { /* fall back to a straight line */ }
    const a = this.network.getPosition(e.subject), b = this.network.getPosition(e.object);
    return { x: a.x + (b.x - a.x) * pos, y: a.y + (b.y - a.y) * pos };
  }

  /**
   * compute_paths: a small "ping" leaves each person and runs out along every edge the path search can
   * actually walk (`rules`: the hub cap and hop limit, as in the backend), hop by hop to the outermost
   * node of its branch (forward only); each edge's colour fades back to normal once its ping arrives,
   * and after a short rest in normal colours the branch's next ping leaves. Each branch loops on its
   * own depth (node-local timer). Edges keep their usual style;
   * the part of an edge the ping has already crossed is painted in a light tint of the path colour, so
   * the search visibly spreads outward. Each edge's
   * ping sets off a little later than its neighbours (random `jit`), so they don't move in lockstep.
   */
  fxPathSearch(rules, hopMs = 800) {
    const { source, target } = this.endpoints;
    const ds = this._searchHops(source, rules), dt = this._searchHops(target, rules);
    const ids = [...new Set([...ds.keys(), ...dt.keys()])];
    const pos = this.network.getPositions(ids);
    const onPath = (id) => this.pathEdges.shortest.has(id) || this.pathEdges.strongest.has(id);
    const pings = [];
    for (const e of this.edges.values()) {
      if (e.origin === "sandbox") continue;
      let best = null;
      for (const [root, d] of [[source, ds], [target, dt]]) {
        const a = d.get(e.subject), b = d.get(e.object);
        if (a === undefined || b === undefined || Math.abs(a - b) !== 1) continue;  // outward edges only
        const [near, far, h] = a < b ? [e.subject, e.object, a] : [e.object, e.subject, b];
        if (!best || h < best.h) best = { id: e.id, root, near, far, h };
      }
      if (best && pos[best.far] && pos[best.root] && !this.hidden.has(best.far) && !this.hidden.has(best.near)) {
        best.jit = Math.random() * PING_JITTER;  // this edge's ping leaves a little late
        best.paint = !onPath(best.id);           // path edges keep their path colour
        pings.push(best);
      }
    }
    if (!pings.length) return;
    // Node-local loops: each branch (a person's edge plus everything grown beyond it) loops on its
    // own depth, so a one-hop fan restarts at once instead of waiting for a deeper sibling.
    pings.sort((a, b) => a.h - b.h);
    const branchOf = new Map(), depth = new Map();
    for (const g of pings) {
      g.branch = g.h === 0 ? g.id : branchOf.get(g.near) ?? g.id;
      if (!branchOf.has(g.far)) branchOf.set(g.far, g.branch);
      depth.set(g.branch, Math.max(depth.get(g.branch) || 0, g.h + 1));
    }
    for (const g of pings) g.cycle = depth.get(g.branch) * hopMs + PING_FADE + PING_REST;  // ms
    this._runFx(Infinity, () => this.network.redraw(), 0);  // redraw every frame: the dots move smoothly
    // One colour, the path colour: the pings ARE the path search. (A rainbow by direction read as noise.)
    const hex = this.colors.strongest.replace("#", "");
    const rgb = hex.length === 6 ? [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16)) : [217, 119, 6];
    this._ping = { pings, t0: performance.now(), hopMs, rgb: rgb.join(",") };
  }

  /**
   * afterDrawing hook: the compute_paths pings. vis has no layer between edges and nodes, so the
   * drawing is clipped to everything outside the visible nodes: trails and dots pass under them.
   */
  _drawPing(ctx) {
    const p = this._ping;
    if (!p) return;
    const now = performance.now() - p.t0;
    const scale = this.network.getScale();
    ctx.save();
    this._clipOutNodes(ctx);  // trails and pellets pass under the entities
    ctx.lineCap = "round";
    const span = 1 - PING_JITTER;  // every ping takes the same time over its edge
    const dots = [];
    for (const g of p.pings) {
      // this branch's own loop (forward only): travel, fade after arriving, rest, again
      const ms = now % g.cycle, x = ms / p.hopMs;  // x: ping position in hops
      const frac = Math.min(1, Math.max(0, (x - g.h - g.jit) / span));
      if (frac <= 0) continue;
      const arrived = (g.h + g.jit + span) * p.hopMs;
      const alpha = ms < arrived ? 1 : 1 - (ms - arrived) / PING_FADE;
      if (alpha <= 0) continue;
      if (g.paint) {  // the crossed part of the edge, in this edge's hue
        const bw = this.network.body.edges[g.id]?.options?.width || 1;
        ctx.beginPath();
        const n = Math.max(2, Math.ceil(frac * 14));
        for (let i = 0; i <= n; i++) {
          const pt = this._edgePoint(g.id, g.near, (frac * i) / n);
          i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y);
        }
        ctx.strokeStyle = `rgba(${p.rgb},${(0.55 * alpha).toFixed(3)})`;
        ctx.lineWidth = Math.max(3, bw);
        ctx.stroke();
      }
      if (frac < 1) dots.push([g, frac]);
    }
    // Flat pellets on top of the painted trails: a short gold capsule aligned with its road, with a
    // thin land-coloured edge so it reads on asphalt and on pale roads alike. No glow, no comet.
    const len = 9 / scale, w = 5 / scale;
    for (const [g, frac] of dots) {
      const head = this._edgePoint(g.id, g.near, frac);
      const back = this._edgePoint(g.id, g.near, Math.max(0, frac - 0.02));
      let dx = head.x - back.x, dy = head.y - back.y;
      const l = Math.hypot(dx, dy);
      if (l < 1e-6) { dx = 1; dy = 0; } else { dx /= l; dy /= l; }
      const tail = { x: head.x - dx * len, y: head.y - dy * len };
      ctx.beginPath();
      ctx.moveTo(tail.x, tail.y);
      ctx.lineTo(head.x, head.y);
      ctx.strokeStyle = this.colors.land;
      ctx.lineWidth = w + 2.5 / scale;
      ctx.stroke();
      ctx.strokeStyle = `rgb(${p.rgb})`;
      ctx.lineWidth = w;
      ctx.stroke();
    }
    ctx.restore();
  }

  /**
   * beforeDrawing hook: the get_search_status beam, a flat translucent wedge in each person's colour
   * turning around that person, under the roads and entities.
   */
  _drawSweep(ctx) {
    const s = this._sweep;
    if (!s) return;
    ctx.save();
    for (const r of s.roots) {
      const c = this.network.getPosition(r.id);
      if (!c || !Number.isFinite(c.x)) continue;
      ctx.beginPath();
      ctx.moveTo(c.x, c.y);
      ctx.arc(c.x, c.y, r.radius, s.beam - s.tail, s.beam);
      ctx.closePath();
      ctx.fillStyle = r.fill;
      ctx.fill();
    }
    ctx.restore();
  }

  /**
   * get_search_status: ONE sweep, as the backend runs one rank_frontier pass over both sides. The beam
   * turns around both people at once and lights, faintly and in that person's colour (teal / orange),
   * exactly the candidates the backend ranks: reached through expanded nodes, not expanded themselves,
   * not hubs, under the hop limit, links still unknown, each on the side it is cheaper to reach from.
   * With `ranked` (the suggestions the agent was shown, recorded in the tool.completed event) the beam
   * sweeps once and the graph then settles on the ranking: the suggested nodes stand out, brightest
   * at #1, each with a "#k" pill. Without it (older recordings) the sweep loops until the next step.
   */
  fxFrontierScan(rules, ranked = null, period = 4500) {
    const { source, target } = this.endpoints;
    const through = new Set([...this.expanded]);
    const ds = this._searchHops(source, rules, through), dt = this._searchHops(target, rules, through);
    const known = new Map();
    for (const e of this.edges.values()) for (const q of [e.subject, e.object]) known.set(q, (known.get(q) || 0) + 1);
    const root = new Map(), cands = [];
    for (const id of new Set([...ds.keys(), ...dt.keys()])) {
      const a = ds.get(id) ?? Infinity, b = dt.get(id) ?? Infinity, hops = Math.min(a, b);
      const n = this.nodes.get(id);
      if (!n || this.expanded.has(id) || id === source || id === target) continue;
      if (hops >= rules.maxHops || rules.isHub(n) || (known.get(id) || 0) >= (n.degree || 0)) continue;
      root.set(id, a <= b ? source : target);
      cands.push(id);
    }
    // The cap never hides what this step is about: every candidate the sweep lights and every
    // suggestion the agent was shown stay drawn for the whole step.
    this._fxKeep = new Set([...cands, ...(Array.isArray(ranked) ? ranked.map((s) => s.qid) : [])]);
    this.applyCap();
    const ids = [...new Set([...cands, source, target])];
    // Each side in its person's colour, as solid pale tints: many candidates light up, so the sweep
    // stays soft and the strong highlight is kept for the ranked few that follow.
    const side = { [source]: [this.colors.source, mix(this.colors.source, this.colors.land, 0.5)],
                   [target]: [this.colors.target, mix(this.colors.target, this.colors.land, 0.5)] };
    const tail = 1.1, lit = new Set();
    const once = Array.isArray(ranked);
    // the beam's wedge reaches just past each person's farthest candidate
    const pos0 = this.network.getPositions(ids), reach = { [source]: 160, [target]: 160 };
    for (const id of cands) {
      const c = pos0[root.get(id)], p = pos0[id];
      if (c && p) reach[root.get(id)] = Math.max(reach[root.get(id)], Math.hypot(p.x - c.x, p.y - c.y) + 60);
    }
    const roots = [source, target].filter((id) => this.nodesDS.get(id))
      .map((id) => ({ id, radius: reach[id], fill: `rgba(${hexRgb(this.colors[id === source ? "source" : "target"])},0.09)` }));
    const frame = (t, ms) => {
      const pos = this.network.getPositions(ids);
      // once: a single turn plus the tail, so the last candidates fade before the ranking appears
      const beam = once ? t * (2 * Math.PI + tail) : ((ms / period) * 2 * Math.PI) % (2 * Math.PI);
      this._sweep = once && t >= 1 ? null : { beam, tail: 0.7, roots };
      const updates = [];
      for (const id of cands) {
        let behind = beam - this._angle(pos, root.get(id), pos[id]);
        behind = once ? behind : (behind + 2 * Math.PI) % (2 * Math.PI);
        const st = this._nodeStyle(this.nodes.get(id));
        if (behind >= 0 && behind < tail) {
          const glow = 1 - behind / tail;  // strongest right under the beam, easing off behind it
          const [ring, fill] = side[root.get(id)];
          updates.push({ id, opacity: Math.max(st.opacity, 0.6 + 0.4 * glow), borderWidth: 1 + 2.6 * glow, size: st.size + 2 * glow,
                         color: { ...st.color, border: ring, background: glow > 0.3 ? fill : st.color.background } });
          lit.add(id);
        } else if (lit.has(id)) {
          lit.delete(id);
          updates.push({ id, opacity: st.opacity, borderWidth: st.borderWidth, size: st.size, color: st.color });
        }
      }
      if (updates.length) this.nodesDS.update(updates);
      else this.network.redraw();  // the wedge still moves
    };
    if (!once) { this._runFx(Infinity, frame); return; }
    const shown = ranked.filter((s) => this.nodes.has(s.qid) && !this.hidden.has(s.qid));
    this._runFx(RANK_SWEEP, frame, 40, () => {
      this._sweep = null;
      if (!shown.length) return;
      this._ranks = new Map(shown.map((s) => [s.qid, s]));
      this._ranksT0 = performance.now();
      // the "#k" pills pop in one after another, in rank order; redraw until the last has landed
      const until = this._ranksT0 + (shown.length - 1) * RANK_STAGGER + RANK_POP;
      const tick = (now) => {
        this.network.redraw();
        this._rankRaf = now < until ? requestAnimationFrame(tick) : null;
      };
      this._rankRaf = requestAnimationFrame(tick);
    });
  }

  /** How a ranked suggestion looks: mint, the higher the rank the bigger and stronger. */
  _rankedStyle(n, sug, name, orderRank, c) {
    const k = Math.max(1, this._ranks.size), strength = 1 - (0.55 * (sug.rank - 1)) / Math.max(1, k - 1);
    const big = this._role(n) === "source" || this._role(n) === "target";
    const base = big ? GraphView.ENDPOINT_SIZE : GraphView.entitySize(n, this.story ? true : null);
    const fontSize = this.story ? 14 * this.labelZoom : 14;
    this.look.set(n.id, { ...this.look.get(n.id), kind: big ? this._role(n) : n.kind, dim: false, plain: false, halo: false, above: false, font: fontSize,
                          bold: sug.rank <= 3, caps: !big && n.kind !== "person", badge: orderRank || null });
    return {
      label: name,
      color: { background: mix(this.colors.fresh, this.colors.land, 0.45 * (1 - strength)), border: this.colors.fresh,
               highlight: { background: c, border: this.colors.select }, hover: { background: c, border: this.colors.select } },
      borderWidth: 2.6 + 1.4 * strength,
      shapeProperties: { borderDashes: false },
      size: base + 1 + 3 * strength,
      font: { size: fontSize },
      opacity: 0.75 + 0.25 * strength,
    };
  }

  /** afterDrawing hook: the "#k" tags on ranked suggestions (constant size on screen), popping in. */
  _drawRanks(ctx) {
    if (!this._ranks) return;
    const scale = this.network.getScale(), now = performance.now();
    const px = 11.5 / scale, padX = 5 / scale, h = 17 / scale;
    ctx.save();
    ctx.font = `800 ${px}px ${FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const [id, sug] of this._ranks) {
      const bn = this.network.body.nodes[id];
      if (!bn || bn.options.hidden) continue;
      const a = REDUCED ? 1 : clamp01((now - this._ranksT0 - (sug.rank - 1) * RANK_STAGGER) / RANK_POP);
      if (a <= 0) continue;
      const e = easeOutBack(a), text = `#${sug.rank}`;
      const rad = this.look.get(id)?.r || bn.options.size || 12;
      const w = ctx.measureText(text).width + 2 * padX;
      const x = bn.x + rad * 0.85 + w / 2, y = bn.y - rad * 0.85;
      ctx.globalAlpha = Math.min(1, a * 1.6);
      ctx.save();
      ctx.translate(x, y);
      ctx.scale(e, e);
      ctx.beginPath();
      ctx.roundRect(-w / 2, -h / 2, w, h, h * 0.42);
      ctx.fillStyle = this.colors.fresh;
      ctx.fill();
      ctx.strokeStyle = this.colors.land;
      ctx.lineWidth = 2 / scale;
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.fillText(text, 0, 0.5 / scale);
      ctx.restore();
    }
    ctx.restore();
  }

  /** Fit every visible node in the area below the step card (a decision about the whole search). */
  allVisibleIds() {
    return [...this.nodes.keys()].filter((id) => !this.hidden.has(id));
  }

  stopCamera() {
    if (this._camRaf) cancelAnimationFrame(this._camRaf);
    this._camRaf = null;
  }

  /**
   * The canvas area the step card leaves free: {top, left, width, height, bottom}, where `bottom` keeps
   * the legend clear. `inset` is {top, left} or a number (a top inset).
   */
  _freeBox(inset) {
    const { width, height } = this.container.getBoundingClientRect();
    const { top = 0, left = 0 } = typeof inset === "number" ? { top: inset } : inset || {};
    // Never let a card squeeze the free area to nothing (e.g. a very narrow window).
    return { top: Math.min(top, height * 0.6), left: Math.min(left, width * 0.6), width, height, bottom: 40 };
  }

  /**
   * {scale, position} that fits `ids` into the free area of `box` (see _freeBox).
   * With `center`, the frame is centred on those nodes and made symmetric around them.
   */
  _frameTarget(ids, box, center = []) {
    const shown = ids.filter((id) => this.nodesDS.get(id) && !this.hidden.has(id));
    if (!shown.length) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const { x, y } of shown.map((id) => this._finalPosition(id))) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
    const mid = center.filter((id) => this.nodesDS.get(id)).map((id) => this._finalPosition(id));
    if (mid.length) {
      const cx = mid.reduce((s, p) => s + p.x, 0) / mid.length, cy = mid.reduce((s, p) => s + p.y, 0) / mid.length;
      // 40% headroom: released leaves drift a little past the ring they grew to.
      const hx = 1.4 * Math.max(cx - x0, x1 - cx, 60), hy = 1.4 * Math.max(cy - y0, y1 - cy, 60);
      x0 = cx - hx; x1 = cx + hx; y0 = cy - hy; y1 = cy + hy;
    }
    const { top, left, width, height, bottom } = box;
    // Sideways a node needs room for its label (centred under it, often wider than the node): the same
    // 110 px margin _inView checks, else a label slid behind the step card on the left.
    const pad = 70, padX = 110, freeW = Math.max(200, width - left), freeH = Math.max(150, height - top - bottom);
    const scale = Math.max(0.15, Math.min(1.1, freeW / (x1 - x0 + 2 * padX), freeH / (y1 - y0 + 2 * pad)));
    // moveTo centres `position` in the canvas; shift it so the frame sits in the free area.
    return { scale, position: { x: (x0 + x1) / 2 - left / 2 / scale, y: (y0 + y1) / 2 - (top - bottom) / 2 / scale } };
  }

  fitTo(nodeIds) {
    const ids = (nodeIds ? [...nodeIds] : []).filter((id) => this.nodesDS.get(id) && !this.hidden.has(id));
    // vis-network throws "Nodes has to be an array of ids" for `nodes: undefined`: omit the key to fit all.
    const options = { animation: { duration: MOTION.camera, easingFunction: MOTION.easing } };  // same glide as the step camera
    if (ids.length) options.nodes = ids;
    this.network.fit(options);
  }
}
