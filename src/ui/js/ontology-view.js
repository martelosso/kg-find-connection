// The Ontology view: the schema diagram (one vis.Network, created once, never rebuilt) and the
// read-only ontology Turtle, with the same two-way highlight as the data view:
// click an arrow or class -> its Turtle blocks are marked; select Turtle lines -> arrows/classes light up.
//
// Colours keep their meaning from the data view: entity kinds use the --c-* kind colours, arrows are
// roads (thicker = stronger relation in the policy), the strongest path's rules are gold, the
// selection is ink. A relation group switched off for the run is a pale dashed arrow.

import { drawEntity } from "./glyphs.js";
import { defineTurtleMode } from "./turtle-parser.js";
import { OTHER_PERSON, ontologyIndex, schemaGraph, schemaToSpans, selectionToSchema } from "./ontology.js";

const FONT = '"Nunito Sans", -apple-system, "Segoe UI", system-ui, sans-serif';
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const KIND_VAR = { person: "--c-person", organization: "--c-org", work: "--c-work", place: "--c-place",
                   position: "--c-position", other: "--c-other" };

export class OntologyView {
  constructor(diagramEl, sourceEl, { onSelect } = {}) {
    this.onSelect = onSelect;
    this.onto = null;
    this.schema = null;
    this.index = null;
    this.selected = { classes: [], arrows: [] };
    this.marks = [];
    this.colors = {
      road: css("--road"), roadMid: css("--road-mid"), roadDim: css("--road-dim"), select: css("--c-select"),
      strongest: css("--c-strongest"), land: css("--bg"), text: css("--text"), muted: css("--muted"), muted2: css("--muted2"),
    };
    this.nodesDS = new vis.DataSet();
    this.edgesDS = new vis.DataSet();
    this.network = new vis.Network(diagramEl, { nodes: this.nodesDS, edges: this.edgesDS }, {
      physics: false,
      interaction: { hover: true, dragNodes: false, zoomView: true, dragView: true, selectConnectedEdges: false },
      nodes: { shape: "custom", ctxRenderer: (a) => this._renderNode(a), size: 30 },
      edges: { arrows: { to: { enabled: true, scaleFactor: 0.7 } }, smooth: false,
               font: { face: FONT, size: 15, multi: "html", color: this.colors.text, strokeWidth: 4,
                       strokeColor: this.colors.land, bold: { face: FONT, size: 16, mod: "bold" } } },
    });
    this.network.on("click", (p) => {
      const cls = [...new Set(p.nodes.map((id) => (id === OTHER_PERSON.id ? "Person" : String(id))))];
      const sel = { classes: cls, arrows: cls.length ? [] : p.edges.map(String) };
      this.select(sel);
      this.onSelect?.(sel);
    });
    this.network.on("hoverNode", () => { diagramEl.style.cursor = "pointer"; });
    this.network.on("hoverEdge", () => { diagramEl.style.cursor = "pointer"; });
    this.network.on("blurNode", () => { diagramEl.style.cursor = ""; });
    this.network.on("blurEdge", () => { diagramEl.style.cursor = ""; });

    if (!CodeMirror.modes.turtle) defineTurtleMode(CodeMirror);
    this.cm = CodeMirror(sourceEl, { value: "", mode: "turtle", theme: "turtle-light", lineNumbers: true,
                                     readOnly: true, lineWrapping: false, viewportMargin: 50 });
    this._suppress = false;
    this._timer = 0;
    this.cm.on("cursorActivity", () => this._onCursor());
  }

  get loaded() { return !!this.onto; }

  /** The /api/people-ontology payload: Turtle text into the editor, index for highlighting. */
  load(onto) {
    this.onto = onto;
    this.cm.setValue(onto.text);
    try { this.index = ontologyIndex(onto.text, onto); } catch { this.index = null; }
  }

  /** (Re)draw the diagram for a run's enabled groups and the PIDs its strongest path uses. */
  render({ enabled = null, used = new Set() } = {}) {
    if (!this.onto) return;
    this.schema = schemaGraph(this.onto, enabled, used);
    const nodes = this.schema.nodes.map((n) => ({ id: n.id, x: n.x, y: n.y, fixed: true, label: n.label,
      title: `${n.label} · ${n.comment}`, kind: n.kind }));
    const person = nodes.find((n) => n.id === "Person");
    if (person) nodes.push({ ...person, ...OTHER_PERSON, label: "Person", title: "Person (the other end of a person-to-person relation)" });
    const pairCount = new Map();
    const edges = this.schema.arrows.map((a) => {
      const sel = this.selected.arrows.includes(a.id);
      // Gold (used by the strongest path) wins over the selection ink; a selected arrow is drawn thicker.
      const color = a.used ? this.colors.strongest : sel ? this.colors.select : a.enabled ? this.colors.road : this.colors.roadDim;
      const width = (a.enabled ? 1.2 + 5 * a.strength : 1.5) + (a.used ? 2 : 0) + (sel ? 2 : 0);
      const [head, ...rest] = a.lines;
      const e = { id: a.id, from: a.from, to: a.to, width, color: { color, highlight: a.used ? this.colors.strongest : this.colors.select, hover: a.used ? this.colors.strongest : this.colors.select },
        dashes: a.enabled ? false : [7, 6],
        label: `<b>${head}</b>${a.enabled ? "" : " (off)"}\n${rest.join("\n")}`,
        font: { color: a.enabled || sel ? this.colors.text : this.colors.muted2 },
        title: `${a.groupLabel}: ${a.properties.map((p) => p.label).join(", ")} · ${a.linkLabel.toLowerCase()} · ${a.word} link`
          + (a.enabled ? "" : " · switched off for this run") };
      if (a.from === a.to) e.to = OTHER_PERSON.id;
      const key = [e.from, e.to].sort().join("|");
      const k = pairCount.get(key) || 0;
      pairCount.set(key, k + 1);
      // Parallel arrows bow apart; the person-to-person ones bow far, so their labels never touch.
      // vis bends "clockwise" relative to the arrow's own direction, so a reversed arrow is flipped to
      // keep alternating sides (Organization: party below, founded by above, a little).
      const up = e.to === OTHER_PERSON.id;
      const bow = up ? 0.95 : 0.3;
      const cw = (k % 2 === 1) === (up || e.from === key.split("|")[0]);
      e.smooth = k === 0 ? false : { enabled: true, type: cw ? "curvedCW" : "curvedCCW", roundness: bow * Math.ceil(k / 2) };
      return e;
    });
    this.nodesDS.update(nodes);
    const keep = new Set(edges.map((e) => e.id));
    this.edgesDS.remove(this.edgesDS.getIds().filter((id) => !keep.has(id)));
    this.edgesDS.update(edges);
    this.network.setSelection({ nodes: this.selected.classes.flatMap((c) => (c === "Person" ? [c, OTHER_PERSON.id] : [c])), edges: this.selected.arrows }, { unselectAll: true });
  }

  _renderNode({ ctx, id, x, y, state, label }) {
    const n = this.nodesDS.get(id);
    const kind = n?.kind || "other";
    const r = kind === "person" ? 34 : 30;
    const fill = css(KIND_VAR[kind] || "--c-other");
    return {
      drawNode: () => {
        drawEntity(ctx, { kind, x, y, r, fill, lip: true,
                          ring: state.selected || state.hover ? this.colors.select : null, ringWidth: 3, ringGap: 4 });
      },
      drawExternalLabel: () => {
        const fs = 17;
        ctx.font = `800 ${fs}px ${FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.lineJoin = "round";
        const ty = y + r + 8;
        ctx.strokeStyle = this.colors.land;
        ctx.lineWidth = 5;
        ctx.strokeText(label, x, ty);
        ctx.fillStyle = this.colors.text;
        ctx.fillText(label, x, ty);
      },
      nodeDimensions: { width: 2 * r, height: 2 * r },
    };
  }

  /** Highlight classes/arrows in the diagram and mark their Turtle blocks. */
  select({ classes = [], arrows = [] } = {}) {
    this.selected = { classes: [...classes], arrows: [...arrows] };
    this.render({ enabled: this.schema?.enabled, used: this._used || new Set() });
    this._markSource();
  }

  /** Keep the run's state (enabled groups, PIDs on the strongest path) for later re-renders. */
  setRun({ enabled = null, used = new Set() } = {}) {
    this._used = used;
    this.render({ enabled, used });
  }

  _markSource() {
    this.marks.forEach((m) => m.clear());
    this.marks = [];
    if (!this.schema) return;
    const { spans, stale } = schemaToSpans(this.index, this.schema, this.selected, this.cm.getValue());
    if (stale || !spans.length) return;
    for (const [a, b] of spans) {
      this.marks.push(this.cm.markText(this.cm.posFromIndex(a), this.cm.posFromIndex(b), { className: "graph-sel" }));
    }
    this._suppress = true;
    this.cm.scrollIntoView({ from: this.cm.posFromIndex(spans[0][0]), to: this.cm.posFromIndex(spans[0][1]) }, 80);
    setTimeout(() => { this._suppress = false; }, 0);
  }

  _onCursor() {
    if (this._suppress || !this.schema) return;
    clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      const ranges = this.cm.listSelections().filter((r) => !r.empty())
        .map((r) => [this.cm.indexFromPos(r.from()), this.cm.indexFromPos(r.to())].sort((a, b) => a - b));
      if (!ranges.length) return;
      const hit = selectionToSchema(this.index, this.schema, ranges, this.cm.getValue());
      if (hit.stale) return;
      const sel = { classes: [...hit.classes], arrows: [...hit.arrows] };
      this.selected = sel;
      this.render({ enabled: this.schema.enabled, used: this._used || new Set() });
      this.onSelect?.(sel, { fromSource: true });
    }, 16);
  }

  /**
   * Called after the view becomes visible (vis and CodeMirror measure their containers). `insetLeft`
   * is the width the caption card covers: the diagram is fitted into the space beside it.
   */
  shown(insetLeft = 0) {
    this.cm.refresh();
    this.network.redraw();
    // Our own fit: vis caps fit() at 100% zoom, and the diagram must sit beside the caption card.
    const box = this.network.body.container.getBoundingClientRect();
    const pos = Object.values(this.network.getPositions());
    if (!pos.length || !box.width) return;
    const xs = pos.map((p) => p.x), ys = pos.map((p) => p.y);
    const padX = 190, padY = 90;  // arrow labels and class names reach beyond the shapes
    const x0 = Math.min(...xs) - padX, x1 = Math.max(...xs) + padX, y0 = Math.min(...ys) - padY, y1 = Math.max(...ys) + padY;
    const free = Math.max(240, box.width - insetLeft);
    const scale = Math.min(1.8, free / (x1 - x0), box.height / (y1 - y0)) * 0.96;
    this.network.moveTo({ position: { x: (x0 + x1) / 2, y: (y0 + y1) / 2 }, scale, offset: { x: insetLeft / 2, y: 0 }, animation: false });
  }

}
