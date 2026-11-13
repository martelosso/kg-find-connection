// Workbench wiring: event stream -> model -> graph / timeline / paths / Turtle, plus the three
// highlight links (graph <-> Turtle, editor selection -> graph, timeline step -> fetched edges),
// path toggles, sandbox overlay, replay and status indicators.

import { api, streamEvents } from "./api.js";
import { $, el, wikidataLink } from "./dom.js";
import { GraphView } from "./graph-view.js";
import { InvestigationModel, label } from "./investigation.js";
import { PathComparison } from "./path-comparison.js";
import { PeoplePicker } from "./people-picker.js";
import { Timeline } from "./timeline.js";
import { TurtleEditor } from "./turtle-editor.js";

// Fallback only (server unreachable at startup); the real list comes from the policy via /api/health.
const GROUPS = [["family", "Family", true], ["mentorship", "Teacher/student", true],
                ["institutional", "Institutions", true], ["works", "Film / music", true]];
let groupsFromPolicy = false;

const model = new InvestigationModel();
let es = null;
let health = null;
let sandbox = null;          // last overlay response
let pathMode = "both";
let turtleTimer = null;
let turtleStale = false;
let shownEdgeIds = null;     // Wikidata edges in the Turtle the editor was given (null = all)

// ---- views ----------------------------------------------------------------------------------
const graph = new GraphView($("#network"), { onSelect: onGraphSelect });
const timeline = new Timeline($("#timeline"), { onSelectRow: onTimelineRow });
const paths = new PathComparison($("#paths"), { onShow: showPaths });
const editor = new TurtleEditor($("#cm-wrap"), {
  onSelectGraph: onEditorSelect,
  onStatus: (s) => setParseStatus(s),
  onDirty: (dirty) => { $("#src-dirty").hidden = !dirty; $("#apply-sandbox").disabled = !dirty || !canEditSandbox(); },
});
const pickSource = new PeoplePicker($("#pick-source"), { placeholder: "From: name or QID" });
const pickTarget = new PeoplePicker($("#pick-target"), { placeholder: "To: name or QID" });

// Groups that are off by default (the weak ones) sit behind one "Weak links" button so the toolbar
// stays on two rows on a 1366px projector. A weak group that is switched on stays visible.
function renderGroups(groups) {
  let open = false;
  const weak = [];
  const buttons = groups.map(([id, text, on, title]) => {
    const b = el("button", { class: `tog${on ? " on" : ""}`, "data-group": id, type: "button", title: title || text }, text);
    b.addEventListener("click", () => { b.classList.toggle("on"); sync(); });
    if (!on) weak.push(b);
    return b;
  });
  const more = el("button", { class: "tog more", type: "button", title: "Show weak relationship groups (off by default)" });
  more.addEventListener("click", () => { open = !open; sync(); });
  function sync() {
    for (const b of weak) b.hidden = !open && !b.classList.contains("on");
    more.textContent = open ? "Weak links ◂" : `Weak links (${weak.length}) ▸`;
  }
  sync();
  $("#groups").replaceChildren(...buttons, ...(weak.length ? [more] : []));
}
renderGroups(GROUPS);

// ---- startup ----------------------------------------------------------------------------------
async function refreshHealth() {
  try {
    health = await api.health();
    graph.maxVisible = health.ui?.max_visible_nodes || graph.maxVisible;
    if (!groupsFromPolicy && Array.isArray(health.groups) && health.groups.length) {
      groupsFromPolicy = true;  // once: later polls must not reset the presenter's toggles
      renderGroups(health.groups.map((g) => [g.id, g.label, g.enabled_by_default, `${g.label} / ${g.label_es}`]));
    }
    const prov = health.provider ? `${health.provider.name}${health.provider.snapshot ? ` · snapshot ${health.provider.snapshot}` : ""}` : "no people graph installed (replay only)";
    $("#conn").className = "sd";
    $("#conn-text").textContent = "Server online";
    $("#provider").textContent = prov;
    const agentOpt = $("#mode option[value=agent]");
    agentOpt.disabled = !health.agent.available;
    agentOpt.textContent = health.agent.available ? `AI agent (${health.agent.model})` : "AI agent (unavailable: no API key)";
    if (!health.agent.available && $("#mode").value === "agent") $("#mode").value = "deterministic";
    $("#mode option[value=deterministic]").disabled = !health.provider;
  } catch {
    $("#conn").className = "sd err";
    $("#conn-text").textContent = "Server offline";
  }
}

async function loadCases() {
  try {
    const { cases } = await api.demoCases();
    const sel = $("#replay");
    sel.replaceChildren(el("option", { value: "" }, cases.some((c) => c.trace) ? "Replay a demo case…" : "No recorded demo cases yet"),
      ...cases.filter((c) => c.trace).map((c) => el("option", { value: c.id }, c.title)));
    sel.disabled = !cases.some((c) => c.trace);
    window.__demoCases = cases;
  } catch { /* health shows offline */ }
}

refreshHealth(); loadCases();
setInterval(refreshHealth, 15000);

// Deep link: /?source=Q..&target=Q..&mode=agent|deterministic
(async () => {
  const q = new URLSearchParams(location.search);
  for (const [key, picker] of [["source", pickSource], ["target", pickTarget]]) {
    const qid = q.get(key);
    if (!qid) continue;
    try { const res = await api.searchPeople(qid); if (res.matches[0]) picker.set(res.matches[0]); } catch {}
  }
  if (q.get("mode")) $("#mode").value = q.get("mode");
})();

// ---- run control ------------------------------------------------------------------------------
$("#go").addEventListener("click", () => start());
$("#replay").addEventListener("change", () => { if ($("#replay").value) start($("#replay").value); });
$("#cancel").addEventListener("click", async () => { if (model.id) try { await api.cancel(model.id); } catch {} });
$("#reset").addEventListener("click", () => { resetAll(); pickSource.clear(); pickTarget.clear(); setStatus("idle", "Pick two people and press Explore, or choose a replay."); });

function resetAll() {
  es?.close(); es = null;
  model.reset(); graph.reset(); timeline.clear();
  sandbox = null; pathMode = "both"; turtleStale = false;
  paths.update({ comparison: null, sandbox: null, answer: null, status: "idle" });
  editor.setText("", []);
  $("#inspector").hidden = true;
  setBadges(null);
  updateCounts();
}

async function start(replayCase) {
  let body;
  if (replayCase) body = { mode: "replay", case_id: replayCase, replay_delay_seconds: 0.7 };
  else {
    if (!pickSource.value || !pickTarget.value) { setStatus("error", "Pick both people from the suggestions first."); return; }
    const groups = [...document.querySelectorAll("#groups .tog.on")].map((b) => b.dataset.group);
    if (!groups.length) { setStatus("error", "Enable at least one relationship group."); return; }
    body = { source_qid: pickSource.value.qid, target_qid: pickTarget.value.qid, mode: $("#mode").value, groups };
  }
  setStatus("busy", replayCase ? "Loading replay…" : "Starting…");
  try {
    const inv = await api.start(body);
    // Only clear the screen once the server has accepted the new run: a failed start (server down,
    // agent unavailable, bad input) must leave the previous result visible for the presenter.
    resetAll();
    model.id = inv.investigation_id;
    setRunning(true);
    es = streamEvents(inv.investigation_id, onEvent, () => { if (model.status === "running") setStatus("error", "Event stream lost; the server may be offline."); });
  } catch (err) {
    setStatus("error", `Could not start: ${err.message}`);
  } finally {
    $("#replay").value = "";
  }
}

function setRunning(on) {
  $("#go").disabled = on; $("#cancel").disabled = !on; $("#replay").disabled = on || !(window.__demoCases || []).some((c) => c.trace);
  $("#apply-sandbox").disabled = !editor.isDirty || !canEditSandbox();
  $("#clear-sandbox").disabled = !sandbox?.sandbox_edges?.length || on;
}

// ---- events -------------------------------------------------------------------------------------
function onEvent(ev) {
  const change = model.apply(ev);
  if (change.type === "duplicate") return;
  const p = ev.payload;
  switch (ev.type) {
    case "investigation.started":
      setBadges(p);
      setStatus("busy", p.run_mode === "replay" ? `Replaying recorded case “${p.replay_of}” (recorded ${fmtTime(p.recorded_at)})` : "Exploring…");
      paths.update({ status: "running" });
      break;
    case "identity.resolved":
      graph.addNodes(change.addedNodes.map((id) => model.nodes.get(id)));
      graph.setEndpoints(p.source.id, p.target.id);
      pickSource.value || pickSource.set({ qid: p.source.id, ...p.source });
      pickTarget.value || pickTarget.set({ qid: p.target.id, ...p.target });
      graph.fitTo();
      break;
    case "graph.expanded":
      graph.markExpanded(p.expanded_qid);
      graph.addNodes(change.addedNodes.map((id) => model.nodes.get(id)));
      graph.addEdges(change.addedEdges.map((id) => model.edges.get(id)), { fresh: true });
      graph.applyCap();
      scheduleTurtle();
      break;
    case "paths.updated":
      graph.setPaths(model.comparison);
      paths.update({ comparison: model.comparison });
      break;
    default:
      if (model.final && ev.type.startsWith("investigation.") && ev.type !== "investigation.started") finish();
  }
  timeline.render(change.row);
  // A tool row nested under an agent decision is re-rendered when its result arrives.
  updateCounts();
}

function finish() {
  const f = model.final;
  setRunning(false);
  graph.setPaths(model.comparison);
  paths.update({ comparison: model.comparison, answer: f.answer || null, status: model.status });
  if (model.status === "failed") setStatus("error", `Failed: ${f.error?.message}`);
  else {
    const found = f.outcome === "path_found";
    setStatus(model.status === "completed" ? "ok" : "warn",
      `${model.status === "completed" ? "Completed" : "Partial result"}: ${found ? "connection found" : "no connection found"} `
      + `(${String(f.stop_reason).replaceAll("_", " ")}) · among explored relationships only`);
  }
  graph.fitWhenSettled(model.comparison?.shortest?.result ? model.pathNodeIds() : null);
  loadTurtle();
}

// ---- Turtle --------------------------------------------------------------------------------------
function scheduleTurtle() {
  clearTimeout(turtleTimer);
  turtleTimer = setTimeout(loadTurtle, 700);
}

async function loadTurtle(force = false) {
  if (!model.id) return;
  if (editor.isDirty && !force) { turtleStale = true; $("#src-stale").hidden = false; return; }
  try {
    const hiddenCount = graph.hidden.size;
    const ids = hiddenCount ? graph.visibleEdgeIds() : null;
    const proj = await api.turtle(model.id, ids);
    shownEdgeIds = ids;
    editor.setText(proj.text, allEdges());
    turtleStale = false; $("#src-stale").hidden = true;
    $("#src-scope").textContent = hiddenCount ? `visible part (${hiddenCount} nodes hidden)` : "explored graph";
  } catch (err) { setParseStatus({ state: "error", message: err.message }); }
}
$("#src-reload").addEventListener("click", () => loadTurtle(true));

function allEdges() {
  return [...model.edges.values(), ...(sandbox?.sandbox_edges || [])];
}

function setParseStatus(s) {
  const dot = $("#parse-dot"), text = $("#parse-text");
  dot.className = `sd${s.state === "error" ? " err" : s.state === "parsing" ? " busy" : ""}`;
  text.textContent = s.state === "ok" ? `${s.triples} triples` : s.state === "parsing" ? "Parsing…" : `Highlighting unavailable: ${s.message}`;
}

// ---- highlight links ----------------------------------------------------------------------------
function onGraphSelect(sel) {
  timeline.select(null);
  graph.setFocus(null);
  if (!sel.nodes.length && !sel.edges.length) { editor.clearMarks(); $("#inspector").hidden = true; return; }
  const ok = editor.showGraphSelection(sel);
  if (!ok) setParseStatus({ state: "error", message: "the editor text changed since it was parsed; reload or wait" });
  showInspector(sel);
}

function onEditorSelect(hit) {
  if (!hit || (!hit.nodes.size && !hit.edges.size)) { graph.setFocus(null); return; }
  timeline.select(null);
  graph.setFocus(hit);
  graph.select({ nodes: [...hit.nodes], edges: [...hit.edges] });
}

function onTimelineRow(row) {
  if (!row) { graph.setFocus(null); editor.clearMarks(); return; }
  graph.setFocus({ edges: row.edgeIds });
  editor.showGraphSelection({ edges: row.edgeIds });
  const nodes = new Set(row.edgeIds.flatMap((id) => { const e = model.edges.get(id); return e ? [e.subject, e.object] : []; }));
  graph.fitTo(nodes);
}

function showPaths(mode) {
  pathMode = mode;
  const useSandbox = paths.tab === "sandbox" && sandbox?.comparison;
  graph.setPaths(model.comparison, useSandbox ? sandbox.comparison : null);
  graph.setMode(mode);
  const ids = mode === "none" ? [] : (() => {
    const c = useSandbox ? sandbox.comparison : model.comparison;
    const pick = (o) => (o?.result ? o.result.hops.map((h) => h.edge_id) : []);
    return [...(mode !== "strongest" ? pick(c?.shortest) : []), ...(mode !== "shortest" ? pick(c?.strongest) : [])];
  })();
  editor.showGraphSelection({ edges: ids });
  if (ids.length) graph.fitTo(new Set(ids.flatMap((id) => { const e = graph.edges.get(id); return e ? [e.subject, e.object] : []; })));
}

function showInspector({ nodes, edges }) {
  const box = $("#inspector");
  box.hidden = false;
  if (edges.length && !nodes.length) {
    const e = graph.edges.get(edges[0]);
    if (!e) return;
    const s = model.nodes.get(e.subject), o = model.nodes.get(e.object);
    box.replaceChildren(
      el("div", { class: "insp-head" }, e.origin === "sandbox" ? "Sandbox edge (not Wikidata)" : "Wikidata statement",
        el("button", { class: "x", on: { click: () => { box.hidden = true; } } }, "×")),
      el("div", {}, wikidataLink(e.subject, label(s) || e.subject), " ",
        el("b", {}, "—", e.origin === "sandbox" ? e.label : wikidataLink(e.property_url, e.label), "→"), " ",
        wikidataLink(e.object, label(o) || e.object)),
      el("div", { class: "small muted mono" }, `${e.subject} ${e.origin === "sandbox" ? "sbx" : "wdt"}:${e.predicate} ${e.object}`),
      el("div", { class: "small muted" }, `group ${e.group || "?"} · source ${e.source} · edge ${e.id}`));
  } else {
    const n = model.nodes.get(nodes[0]);
    if (!n) return;
    const known = [...model.edges.values()].filter((e) => e.subject === n.id || e.object === n.id).length;
    box.replaceChildren(
      el("div", { class: "insp-head" }, n.kind, el("button", { class: "x", on: { click: () => { box.hidden = true; } } }, "×")),
      el("div", {}, el("b", {}, label(n)), " ", wikidataLink(n.id, n.id)),
      n.label_es && n.label_es !== n.label_en ? el("div", { class: "small muted" }, `es: ${n.label_es}`) : null,
      el("div", { class: "small muted" }, `${n.degree} links in the snapshot · ${known} in the explored graph`
        + `${model.expanded.has(n.id) ? " · expanded" : ""}`));
  }
}

// ---- sandbox -----------------------------------------------------------------------------------
function canEditSandbox() { return !!model.id && model.status !== "running" && model.status !== "idle"; }

$("#add-sandbox").addEventListener("click", () => {
  const sel = graph.network.getSelectedNodes();
  const [a, b] = sel.length >= 2 ? sel : [model.source?.id || "Q1", model.target?.id || "Q2"];
  const line = `wd:${a} sbx:P1066 wd:${b} .  # hypothetical: change P1066 (student of) to any allowed PID\n`;
  const cm = editor.cm;
  const doc = cm.getDoc();
  const marker = "# ── Sandbox edits";
  const idx = cm.getValue().indexOf(marker);
  const pos = idx >= 0 ? doc.posFromIndex(cm.getValue().indexOf("\n", idx) + 1) : { line: doc.lastLine() + 1, ch: 0 };
  if (idx < 0) doc.replaceRange(`\n# ── Sandbox edits (presenter overlay, NOT Wikidata) ──\n`, { line: doc.lastLine() + 1, ch: 0 });
  doc.replaceRange(line, idx >= 0 ? pos : { line: doc.lastLine() + 1, ch: 0 });
  cm.focus();
});

$("#apply-sandbox").addEventListener("click", async () => {
  try {
    const res = await api.setOverlay(model.id, editor.text, shownEdgeIds);
    applySandbox(res);
  } catch (err) { setStatus("error", `Sandbox rejected: ${err.message}`); }
});

$("#clear-sandbox").addEventListener("click", async () => {
  try { applySandbox(await api.clearOverlay(model.id)); } catch (err) { setStatus("error", err.message); }
});

function applySandbox(res) {
  const old = sandbox?.sandbox_edges?.map((e) => e.id) || [];
  if (old.length) graph.removeEdges(old);
  sandbox = res;
  graph.addEdges(res.sandbox_edges);
  paths.update({ sandbox: res.sandbox_edges.length ? res : null });
  if (res.sandbox_edges.length) { paths.tab = "sandbox"; paths.render(); showPaths("both"); }
  else { paths.tab = "shortest"; paths.render(); showPaths(pathMode); }
  $("#clear-sandbox").disabled = !res.sandbox_edges.length;
  const warn = res.warnings.length ? ` · ${res.warnings.length} warning${res.warnings.length === 1 ? "" : "s"} (see Paths)` : "";
  setStatus(res.sandbox_edges.length ? "warn" : "ok", res.sandbox_edges.length
    ? `${res.sandbox_edges.length} sandbox edge${res.sandbox_edges.length === 1 ? "" : "s"} active: hypothetical, not Wikidata${warn}`
    : `Sandbox cleared: showing Wikidata-only results${warn}`);
  loadTurtle(true);
}

// ---- status & badges ----------------------------------------------------------------------------
function setStatus(kind, text) {
  $("#run-dot").className = `sd${kind === "error" ? " err" : kind === "busy" ? " busy" : kind === "warn" ? " warn" : kind === "idle" ? " idle" : ""}`;
  $("#run-text").textContent = text;
}

function setBadges(p) {
  const live = $("#badge-mode"), strat = $("#badge-strategy");
  if (!p) { live.className = "badge idle"; live.textContent = "IDLE"; strat.hidden = true; return; }
  const replay = p.run_mode === "replay";
  live.className = `badge ${replay ? "replay" : "live"}`;
  live.textContent = replay ? `REPLAY · recorded ${fmtTime(p.recorded_at)}` : "LIVE";
  live.title = replay ? "Recorded events played back. Not a fresh exploration." : "Running now against the local Wikidata snapshot.";
  strat.hidden = false;
  strat.className = `badge ${p.strategy === "agent" ? "agent" : "det"}`;
  strat.textContent = p.strategy === "agent" ? `AI agent${p.model ? ` · ${p.model}` : ""}` : "Deterministic exploration (no AI)";
}

function updateCounts() {
  const hidden = graph.hidden.size;
  $("#counts").textContent = model.nodes.size ? `${model.nodes.size} nodes · ${model.edges.size} edges · ${model.expanded.size} expanded` : "";
  const btn = $("#show-all");
  btn.hidden = !hidden && !graph.showAll;
  btn.textContent = graph.showAll ? "Cap visible nodes" : `Show all (${hidden} hidden)`;
}
$("#show-all").addEventListener("click", () => { graph.setShowAll(!graph.showAll); updateCounts(); scheduleTurtle(); });
$("#fit").addEventListener("click", () => graph.fitTo());

function fmtTime(iso) { return iso ? String(iso).slice(0, 16).replace("T", " ") : "?"; }

// ---- source pane toggle & drag -------------------------------------------------------------------
$("#toggle-src").addEventListener("click", () => {
  document.body.classList.toggle("no-src");
  $("#toggle-src").classList.toggle("on", !document.body.classList.contains("no-src"));
  setTimeout(() => { editor.refresh(); graph.network.redraw(); }, 50);
});
(() => {
  const drag = $("#drag"), pane = $("#epane");
  let x0 = 0, w0 = 0, on = false;
  drag.addEventListener("mousedown", (e) => { on = true; x0 = e.clientX; w0 = pane.offsetWidth; drag.classList.add("on"); e.preventDefault(); });
  window.addEventListener("mousemove", (e) => { if (on) pane.style.width = `${Math.max(220, Math.min(window.innerWidth * 0.6, w0 + e.clientX - x0))}px`; });
  window.addEventListener("mouseup", () => { if (on) { on = false; drag.classList.remove("on"); editor.refresh(); } });
})();

window.__workbench = { model, graph, editor, timeline, paths };  // handy for debugging and E2E checks
