// Investigation timeline: decisions (agent), tool calls with returned counts / truncation / errors,
// path updates and the final outcome. Rows with edge IDs highlight those edges when clicked.

import { el } from "./dom.js";

export class Timeline {
  constructor(container, { onSelectRow } = {}) {
    this.root = container;
    this.onSelectRow = onSelectRow;
    this.rows = new Map();     // eventId -> element
    this.selected = null;
    this.clear();
  }

  clear() {
    this.rows.clear(); this.selected = null;
    this.root.replaceChildren(el("li", { class: "empty" }, "No investigation yet. Pick two people and press Explore, or choose a replay."));
  }

  render(row) {
    if (!row) return;
    this.root.querySelector(".empty")?.remove();
    const node = this._build(row);
    const old = this.rows.get(row.eventId);
    if (old) old.replaceWith(node);
    else this.root.append(node);
    this.rows.set(row.eventId, node);
    node.scrollIntoView({ block: "nearest" });
  }

  /** Re-render from scratch (step-through: going back a step). */
  renderAll(rows) {
    this.root.classList.add("instant");  // a rebuild (going back a step) should not replay every entrance
    this.clear();
    for (const r of rows) this.render(r);
    requestAnimationFrame(() => requestAnimationFrame(() => this.root.classList.remove("instant")));
  }

  /** Mark the rows of the step being presented (no graph side effects, unlike select()). */
  markCurrent(eventIds) {
    for (const n of this.root.querySelectorAll(".current")) n.classList.remove("current");
    let last = null;
    for (const id of eventIds) { const n = this.rows.get(id); if (n) { n.classList.add("current"); last = n; } }
    last?.scrollIntoView({ block: "nearest" });
  }

  _build(row) {
    const clickable = row.edgeIds?.length > 0;
    const li = el("li", { class: `row ${row.kind}${row.status === "error" ? " error" : ""}${row.parent ? " nested" : ""}${clickable ? " clickable" : ""}${this.selected === row.eventId ? " sel" : ""}`,
                          title: clickable ? "Click to highlight these edges in the graph" : null });
    if (row.kind === "decision") li.append(el("span", { class: "tag" }, "agent"), " ", row.text);
    else if (row.kind === "tool") {
      const target = row.qid ? [" ", el("b", {}, row.label || row.qid), el("span", { class: "muted" }, ` · ${row.side} side`)] : [];
      li.append(el("span", { class: "tool-name" }, row.tool), ...target);
      if (row.reason && !row.parent) li.append(el("div", { class: "muted small" }, row.reason));
      const bits = [];
      if (row.returned !== undefined) bits.push(`${row.returned} returned`, `+${row.addedNodeCount} nodes`, `+${row.addedEdgeCount} edges`);
      if (row.truncated) bits.push(row.capHit ? `cut at ${row.capHit.replaceAll("_", " ")}` : "truncated");
      if (row.status === "running") bits.push("running…");
      if (bits.length) li.append(el("div", { class: `small ${row.truncated ? "warn" : "muted"}` }, bits.join(" · ")));
      if (row.error) li.append(el("div", { class: "small err" }, `✗ ${row.error.code}: ${row.error.message}`));
    } else li.append(row.text);
    if (clickable) li.addEventListener("click", () => this.select(row));
    return li;
  }

  select(row) {
    const same = this.selected === row?.eventId;
    for (const n of this.root.querySelectorAll(".sel")) n.classList.remove("sel");
    this.selected = same || !row ? null : row.eventId;
    if (this.selected) this.rows.get(this.selected)?.classList.add("sel");
    this.onSelectRow?.(this.selected ? row : null);
  }
}
