// Result comparison: Shortest / Strongest tabs (+ Sandbox when an overlay is active), hop lists with
// relationship names, PIDs, per-hop cost and Wikidata links, and the explored-graph qualification.

import { el, wikidataLink } from "./dom.js";

export class PathComparison {
  constructor(container, { onShow } = {}) {
    this.root = container;
    this.onShow = onShow;     // (mode) => void : "shortest" | "strongest" | "both" | "none"
    this.tab = "shortest";
    this.comparison = null;
    this.sandbox = null;      // {comparison, warnings, sandbox_edges}
    this.answer = null;
    this.status = "idle";
    this.render();
  }

  update({ comparison, sandbox, answer, status } = {}) {
    if (comparison !== undefined) this.comparison = comparison;
    if (sandbox !== undefined) this.sandbox = sandbox;
    if (answer !== undefined) this.answer = answer;
    if (status !== undefined) this.status = status;
    this.render();
  }

  render() {
    const c = this.comparison;
    const parts = [];
    if (this.answer) parts.push(this._answer(this.answer));
    if (!c?.shortest?.result) {
      parts.push(el("div", { class: "empty" }, this.status === "running"
        ? "Exploring… paths appear as soon as the explored graph connects the two people."
        : this.status === "idle" ? "Results will appear here." : "No connection among the explored relationships."));
      if (this.sandbox?.comparison?.shortest?.result) parts.push(this._tabs(), this._body());
      this.root.replaceChildren(...parts);
      return;
    }
    parts.push(this._tabs(), this._body());
    this.root.replaceChildren(...parts);
  }

  _tabs() {
    const c = this.comparison, w = c?.strongest?.result, s = c?.shortest?.result;
    const tab = (id, text, sub) => el("button", { class: `ptab${this.tab === id ? " on" : ""}`, "data-tab": id,
      on: { click: () => { this.tab = id; this.onShow?.(id === "sandbox" ? "both" : id); this.render(); } } }, text, sub ? el("small", {}, sub) : null);
    const tabs = [
      tab("shortest", "Shortest", s ? `${s.hop_count} hop${s.hop_count === 1 ? "" : "s"}` : "—"),
      tab("strongest", "Strongest", w ? `cost ${w.total_cost.toFixed(2)}` : "—"),
    ];
    if (this.sandbox?.sandbox_edges?.length) tabs.push(tab("sandbox", "With sandbox", `${this.sandbox.sandbox_edges.length} edit${this.sandbox.sandbox_edges.length === 1 ? "" : "s"}`));
    return el("div", { class: "ptabs" }, ...tabs,
      el("button", { class: "ptab ghost", title: "Show both paths on the graph", on: { click: () => this.onShow?.("both") } }, "Both"),
      el("button", { class: "ptab ghost", title: "Remove path emphasis", on: { click: () => this.onShow?.("none") } }, "Off"));
  }

  _body() {
    const c = this.comparison;
    if (this.tab === "sandbox") {
      const sc = this.sandbox?.comparison;
      return el("div", { class: "pbody" },
        el("div", { class: "banner sandbox" }, "Hypothetical: includes presenter sandbox edges, which are NOT Wikidata statements."),
        sc?.shortest?.result ? [this._card("Shortest with sandbox", sc.shortest.result), sc.same_path ? null : this._card("Strongest with sandbox", sc.strongest.result)]
          : el("div", { class: "empty" }, "No path even with the sandbox edges."),
        this._warnings());
    }
    const r = this.tab === "strongest" ? c.strongest.result : c.shortest.result;
    const same = c.same_path ? el("div", { class: "note" }, "Shortest and strongest are the same path here.") : null;
    return el("div", { class: "pbody" },
      this._card(this.tab === "strongest" ? "Strongest · lowest weighted cost" : "Shortest · fewest relationships", r),
      same,
      el("div", { class: "note" },
        `Scope: ${c.scope === "explored_graph" ? "shortest/strongest among the explored relationships only, not all of Wikidata" : "complete within the configured snapshot"}. `,
        "A shared employer, school, sports team or organization is an association, not proof that people met. ",
        "Statements are what Wikidata records, not verified facts."),
      this._warnings());
  }

  _card(title, r) {
    if (!r) return el("div", { class: "empty" }, "No path.");
    const byQid = Object.fromEntries(r.nodes.map((n) => [n.qid, n]));
    const name = (q) => byQid[q]?.label_en || byQid[q]?.label_es || q;
    return el("div", { class: "pcard" },
      el("div", { class: "pcard-head" }, el("b", {}, title),
        el("span", { class: "muted" }, `${r.hop_count} hop${r.hop_count === 1 ? "" : "s"} · cost ${r.total_cost.toFixed(2)}`)),
      el("ol", { class: "hops" }, r.hops.map((h) => el("li", { class: h.origin === "sandbox" ? "sandbox" : "" },
        wikidataLink(h.from_qid, name(h.from_qid)), " ",
        el("span", { class: "rel" }, "—", wikidataLink(h.property_url, h.label), el("small", {}, ` ${h.pid}`), "→"), " ",
        wikidataLink(h.to_qid, name(h.to_qid)),
        el("div", { class: "small muted" },
          `${h.relation_group}${h.direction === "reverse" ? " · traversed against the recorded direction" : ""} · cost ${h.cost.total.toFixed(2)}`
          + ` (strength ${h.cost.strength.toFixed(2)}${h.cost.hub_penalty ? `, hub +${h.cost.hub_penalty.toFixed(2)}` : ""})`
          + (h.origin === "sandbox" ? " · SANDBOX EDGE" : "")))))
    );
  }

  _answer(a) {
    const agent = a.summary_source === "agent";
    return el("div", { class: `answer${agent ? "" : " backend"}` },
      el("div", { class: "small muted" }, agent ? "Agent summary · checked: mentions only entities on the returned paths"
        : "Summary generated from the backend's verified results"),
      el("div", {}, a.summary),
      a.unverified_mentions?.length ? el("div", { class: "small warn" }, `The agent's own summary was rejected: it mentioned ${a.unverified_mentions.join(", ")}, which are not on any returned path.`) : null);
  }

  _warnings() {
    const w = this.sandbox?.warnings;
    return w?.length ? el("ul", { class: "warnings" }, w.map((x) => el("li", {}, x))) : null;
  }
}
