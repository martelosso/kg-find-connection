// Result: the strongest path (+ a Sandbox tab when an overlay is active), hop list with relationship
// names, PIDs, per-hop cost and Wikidata links, and the explored-graph qualification.
// Demo choice: only the strongest path is shown; the backend still computes the shortest as well.

import { el, wikidataLink } from "./dom.js";

export class PathComparison {
  constructor(container, { onShow } = {}) {
    this.root = container;
    this.onShow = onShow;     // (mode) => void : "strongest" | "none"
    this.tab = "strongest";
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
    if (!c?.strongest?.result) {
      parts.push(el("div", { class: "empty" }, this.status === "running"
        ? "Exploring… a path appears as soon as the explored subgraph connects the two people."
        : this.status === "idle" ? "Results will appear here." : "No path in the explored subgraph."));
      if (this.sandbox?.comparison?.strongest?.result) parts.push(this._tabs(), this._body());
      this.root.replaceChildren(...parts);
      return;
    }
    parts.push(this._tabs(), this._body());
    this.root.replaceChildren(...parts);
  }

  _tabs() {
    const w = this.comparison?.strongest?.result;
    const tab = (id, text, sub) => el("button", { class: `ptab${this.tab === id ? " on" : ""}`, "data-tab": id,
      on: { click: () => { this.tab = id; this.onShow?.("strongest"); this.render(); } } }, text, sub ? el("small", {}, sub) : null);
    const tabs = [tab("strongest", "Strongest path", w ? `${w.hop_count} hop${w.hop_count === 1 ? "" : "s"} · cost ${w.total_cost.toFixed(2)}` : "—")];
    if (this.sandbox?.sandbox_edges?.length) tabs.push(tab("sandbox", "With sandbox", `${this.sandbox.sandbox_edges.length} edit${this.sandbox.sandbox_edges.length === 1 ? "" : "s"}`));
    return el("div", { class: "ptabs" }, ...tabs,
      el("button", { class: "ptab ghost", title: "Show the path on the graph", on: { click: () => this.onShow?.("strongest") } }, "Show"),
      el("button", { class: "ptab ghost", title: "Remove path emphasis", on: { click: () => this.onShow?.("none") } }, "Off"));
  }

  _body() {
    const c = this.comparison;
    if (this.tab === "sandbox") {
      const sc = this.sandbox?.comparison;
      return el("div", { class: "pbody" },
        el("div", { class: "banner sandbox" }, "Hypothetical: includes presenter sandbox edges, which are NOT Wikidata statements."),
        sc?.strongest?.result ? this._card("Strongest with sandbox", sc.strongest.result)
          : el("div", { class: "empty" }, "No path even with the sandbox edges."),
        this._warnings());
    }
    return el("div", { class: "pbody" },
      this._card("Strongest · lowest weighted cost", c.strongest.result),
      el("div", { class: "note" },
        `Scope: ${c.scope === "explored_graph" ? "strongest within the explored subgraph only, not all of Wikidata" : "complete within the configured snapshot"}. `,
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
      el("div", { class: "small muted" }, agent ? "The agent's final answer · grounding check passed: it names only entities on the verified path"
        : "Template answer from the verified path (no LLM)"),
      el("div", {}, a.summary),
      a.unverified_mentions?.length ? el("div", { class: "small warn" }, `Grounding check failed: the agent's answer named ${a.unverified_mentions.join(", ")}, which is not on the verified path, so the template answer is shown instead.`) : null);
  }

  _warnings() {
    const w = this.sandbox?.warnings;
    return w?.length ? el("ul", { class: "warnings" }, w.map((x) => el("li", {}, x))) : null;
  }
}
