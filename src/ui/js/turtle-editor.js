// CodeMirror 5 Turtle editor with two-way selection sync (behaviour ported from live-editor.html).
//
// - setText() loads backend-generated Turtle and marks the editor clean.
// - Every edit re-parses (debounced) and rebuilds the graph<->text index. Until that finishes,
//   index.text !== editor text and all highlighting is skipped: the stale-offset guard.
// - The editor is editable so the presenter can write sandbox statements (sbx:...). Edits never
//   touch Wikidata facts: the server parses the text with RDFLib and only accepts sbx: edges.

import { buildIndex, graphToSpans, selectionToGraph } from "./mapping.js";
import { defineTurtleMode } from "./turtle-parser.js";

let modeDefined = false;

export class TurtleEditor {
  constructor(container, { onSelectGraph, onStatus, onDirty } = {}) {
    if (!modeDefined) { defineTurtleMode(CodeMirror); modeDefined = true; }
    this.cm = CodeMirror(container, {
      value: "", mode: "turtle", theme: "turtle-light", lineNumbers: true, lineWrapping: false,
      tabSize: 2, indentUnit: 2, viewportMargin: 50,
    });
    this.edges = [];
    this.index = null;
    this.cleanText = "";
    this.marks = [];
    this.onSelectGraph = onSelectGraph;
    this.onStatus = onStatus;
    this.onDirty = onDirty;
    this._timer = null;
    this._raf = 0;
    this._suppressSelect = false;
    this.cm.on("changes", () => {
      this.clearMarks();
      this.onDirty?.(this.isDirty);
      clearTimeout(this._timer);
      this.onStatus?.({ state: "parsing" });
      this._timer = setTimeout(() => this.reindex(), 350);
    });
    this.cm.on("cursorActivity", () => this._onCursor());
  }

  get text() { return this.cm.getValue(); }
  get isDirty() { return this.cm.getValue() !== this.cleanText; }

  setEdges(edges) { this.edges = edges; this.reindex(); }

  setText(text, edges) {
    if (edges) this.edges = edges;
    const scroll = this.cm.getScrollInfo();
    this.cleanText = text;
    this.cm.setValue(text);
    this.cm.scrollTo(scroll.left, scroll.top);
    clearTimeout(this._timer);
    this.reindex();
    this.onDirty?.(false);
  }

  reindex() {
    const text = this.cm.getValue();
    try {
      this.index = buildIndex(text, this.edges);
      this.onStatus?.({ state: "ok", triples: this.index.triples });
    } catch (err) {
      this.index = null;
      this.onStatus?.({ state: "error", message: err.message });
    }
  }

  clearMarks() { this.marks.forEach((m) => m.clear()); this.marks = []; }

  /** Graph selection -> mark matching statements. Returns false when offsets are stale. */
  showGraphSelection(sel) {
    this.clearMarks();
    const { spans, stale } = graphToSpans(this.index, sel, this.cm.getValue());
    if (stale || !spans.length) return !stale;
    for (const [a, b] of spans) {
      this.marks.push(this.cm.markText(this.cm.posFromIndex(a), this.cm.posFromIndex(b), { className: "graph-sel" }));
    }
    this._suppressSelect = true;
    this.cm.scrollIntoView({ from: this.cm.posFromIndex(spans[0][0]), to: this.cm.posFromIndex(spans[0][1]) }, 80);
    setTimeout(() => { this._suppressSelect = false; }, 0);
    return true;
  }

  _onCursor() {
    if (this._suppressSelect) return;
    // A timer rather than requestAnimationFrame: rAF is paused in background tabs.
    clearTimeout(this._raf);
    this._raf = setTimeout(() => {
      const ranges = this.cm.listSelections().filter((r) => !r.empty())
        .map((r) => [this.cm.indexFromPos(r.from()), this.cm.indexFromPos(r.to())].sort((a, b) => a - b));
      if (!ranges.length) { this.onSelectGraph?.(null); return; }
      const hit = selectionToGraph(this.index, ranges, this.cm.getValue());
      this.onSelectGraph?.(hit.stale ? null : hit);
    }, 16);
  }

  refresh() { this.cm.refresh(); }
}
