// Tiny DOM helper. Every string is inserted as TEXT nodes (labels come from Wikidata), never parsed as HTML.

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (k === "style") n.setAttribute("style", v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : String(c));
  }
  return n;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function wikidataLink(qidOrUrl, text) {
  const href = qidOrUrl.startsWith("http") ? qidOrUrl : `https://www.wikidata.org/wiki/${qidOrUrl}`;
  return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
}
