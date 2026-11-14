// Person typeahead over /api/people/search (labels en/es, QIDs accepted).

import { api } from "./api.js";
import { el } from "./dom.js";

export class PeoplePicker {
  constructor(root, { placeholder, onPick } = {}) {
    this.onPick = onPick;
    this.value = null;
    this.input = el("input", { type: "text", placeholder, autocomplete: "off", spellcheck: "false" });
    this.hint = el("div", { class: "pick-hint" });
    this.list = el("ul", { class: "pick-list", hidden: true });
    root.append(this.input, this.hint, this.list);
    this.items = []; this.active = -1; this.timer = null; this.seq = 0;
    this.input.addEventListener("input", () => this._query());
    this.input.addEventListener("keydown", (ev) => this._key(ev));
    this.input.addEventListener("blur", () => setTimeout(() => this._close(), 150));
  }

  async _query() {
    clearTimeout(this.timer);
    this.value = null; this.hint.textContent = ""; this.input.title = "";
    const q = this.input.value.trim();
    if (q.length < 2) { this.items = []; this._render(); return; }
    const seq = ++this.seq;
    this.timer = setTimeout(async () => {
      try {
        const res = await api.searchPeople(q);
        if (seq !== this.seq) return;
        this.items = res.matches; this.active = this.items.length ? 0 : -1;
        if (!this.items.length) this.hint.textContent = "No person with that name in the snapshot.";
      } catch (err) { this.items = []; this.hint.textContent = err.message; }
      this._render();
    }, 160);
  }

  _render() {
    this.list.replaceChildren(...this.items.map((m, i) => el("li", { class: i === this.active ? "active" : "", on: { mousedown: () => this.set(m) } },
      el("span", {}, m.label_en || m.label_es || m.qid),
      el("small", {}, ` ${m.qid} · ${m.degree} links${m.label_es && m.label_es !== m.label_en ? ` · es: ${m.label_es}` : ""}`))));
    this.list.hidden = this.items.length === 0;
  }

  _key(ev) {
    if (this.list.hidden) return;
    if (ev.key === "ArrowDown") { this.active = Math.min(this.items.length - 1, this.active + 1); this._render(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { this.active = Math.max(0, this.active - 1); this._render(); ev.preventDefault(); }
    else if (ev.key === "Enter" && this.active >= 0) { this.set(this.items[this.active]); ev.preventDefault(); }
    else if (ev.key === "Escape") this._close();
  }

  _close() { this.list.hidden = true; }

  set(m) {
    this.value = m;
    this.input.value = m.label_en || m.label_es || m.qid;
    // The ID and link count go in a tooltip: a line under the input made the toolbar two rows tall.
    this.hint.textContent = "";
    this.input.title = `${m.qid}${m.degree != null ? ` · ${m.degree} links` : ""}`;
    this._close();
    this.onPick?.(m);
  }

  clear() { this.value = null; this.input.value = ""; this.input.title = ""; this.hint.textContent = ""; }
}
