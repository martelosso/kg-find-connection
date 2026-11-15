// Workbench wiring: event stream -> model -> graph / timeline / paths / Turtle, plus the three
// highlight links (graph <-> Turtle, editor selection -> graph, timeline step -> fetched edges),
// path toggles, sandbox overlay, replay and status indicators.

import { api, streamEvents } from "./api.js";
import { $, el, wikidataLink } from "./dom.js";
import { GraphView } from "./graph-view.js";
import { InvestigationModel, label } from "./investigation.js";
import { PathComparison } from "./path-comparison.js";
import { PeoplePicker } from "./people-picker.js";
import { StepBook, describeStep, describeTail, meetingPoint, orderBadge, pathChain, pathChains, resultHeadline, strengthSummary,
         toolAction, toolPurpose, whyStronger } from "./steps.js";
import { Timeline } from "./timeline.js";
import { TurtleEditor } from "./turtle-editor.js";

// Fallback only (server unreachable at startup); the real list comes from the policy via /api/health.
const GROUPS = [["family", "Family", true], ["mentorship", "Teacher/student", true],
                ["institutional", "Institutions", true], ["works", "Film / music", true]];
let groupsFromPolicy = false;
let optionsFromServer = false;

const model = new InvestigationModel();
let es = null;
let health = null;
let sandbox = null;          // last overlay response
let pathMode = "strongest";
let turtleTimer = null;
let turtleStale = false;
let shownEdgeIds = null;     // Wikidata edges in the Turtle the editor was given (null = all)
// Step-through presentation (plan §2): every run is stepped. Its events are buffered in `book` and
// revealed one step at a time with Next / Back; nothing plays itself out.
let stepRun = false;         // a run is being stepped through
let quietApply = false;      // applying the folded tail: no green flash for its new statements
let stepIndex = -1;          // last revealed step (-1 = none yet)
const book = new StepBook();

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

// Relationship groups live in one "Relations ▾" menu (checkboxes), so the toolbar stays on one row:
// the groups on by default first, then the weak ones. The button shows how many are on.
function renderGroups(groups) {
  // Under the "Weak links" heading the "(weak)" suffix of each label would only repeat it.
  const row = ([id, text, on, title]) => el("label", { class: "menu-item", title: title || text },
    el("input", { type: "checkbox", "data-group": id, checked: on, on: { change: syncGroupCount } }), " ",
    on ? text : String(text).replace(/\s*\(weak\)\s*$/i, ""));
  const strong = groups.filter((g) => g[2]), weak = groups.filter((g) => !g[2]);
  $("#groups").replaceChildren(...strong.map(row),
    weak.length ? el("div", { class: "menu-sep" }, "Weak links (off by default)") : null, ...weak.map(row));
  syncGroupCount();
}
function enabledGroups() { return [...document.querySelectorAll("#groups input[data-group]:checked")].map((b) => b.dataset.group); }
function syncGroupCount() {
  $("#groups-count").textContent = `${enabledGroups().length}/${document.querySelectorAll("#groups input[data-group]").length}`;
}
// A small dropdown: the button toggles the panel; a click outside or Escape closes it.
function dropdown(button, panel) {
  const set = (open) => { panel.hidden = !open; button.setAttribute("aria-expanded", String(open)); button.classList.toggle("on", open); };
  button.addEventListener("click", (e) => { e.stopPropagation(); set(panel.hidden); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => set(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") set(false); });
}
dropdown($("#groups-btn"), $("#groups"));

// Options: the budgets of the NEXT run, each from 1 up to the server's hard ceiling (config/search.yaml
// `limits`). Only values that differ from the defaults are sent; the run records what it used.
// Remembered per browser (a convenience: it may be lost, and nothing depends on it).
const OPTION_FIELDS = [
  // [key, label, hint, agentOnly]
  ["max_agent_tool_calls", "Tool calls", "Every tool call the LLM agent makes counts.", true],
  ["max_agent_turns", "Model turns", "Each turn is one model reply; it may issue several tool calls.", true],
  ["max_provider_requests", "Expansions", "Entities whose statements are fetched (get_neighbors).", false],
  ["max_explored_nodes", "Entities explored", "Size of the explored subgraph.", false],
  ["max_explored_edges", "Statements explored", "", false],
  ["max_edges_per_expansion", "Statements per expansion", "More than this and the expansion is cut (truncated).", false],
  ["wall_time_seconds", "Time limit (s)", "", false],
];
const OPTIONS_KEY = "sixdegrees.options";
let optionValues = {};  // key -> number, only keys the presenter changed
try { optionValues = JSON.parse(localStorage.getItem(OPTIONS_KEY) || "{}") || {}; } catch { optionValues = {}; }

function optionDefaults() {
  const b = health?.budgets;
  return b ? { ...b, max_agent_turns: health.agent_max_turns } : null;
}
function saveOptions() { try { localStorage.setItem(OPTIONS_KEY, JSON.stringify(optionValues)); } catch { /* private window */ } }

function renderOptions() {
  const defs = optionDefaults(), lim = health?.limits;
  if (!defs || !lim) {
    $("#options").replaceChildren(el("div", { class: "menu-sep" }, "Options load when the server is online."));
    return;
  }
  // Drop remembered values the server no longer allows (or that equal the default).
  for (const [k, v] of Object.entries(optionValues)) if (!(k in lim) || v > lim[k] || v < 1 || v === defs[k]) delete optionValues[k];
  const rows = OPTION_FIELDS.map(([k, text, hint, agentOnly]) => {
    const input = el("input", { type: "number", min: "1", max: String(lim[k]), step: k === "wall_time_seconds" ? "5" : "1",
      value: String(optionValues[k] ?? defs[k]), "data-option": k,
      on: { change: (e) => {
        const v = Math.round(Number(e.target.value));
        if (!Number.isFinite(v) || v < 1) { e.target.value = String(optionValues[k] ?? defs[k]); return; }
        const clamped = Math.min(v, lim[k]);
        e.target.value = String(clamped);
        if (clamped === defs[k]) delete optionValues[k]; else optionValues[k] = clamped;
        saveOptions(); syncOptions();
      } } });
    return el("label", { class: "opt-row", title: `${hint ? hint + " " : ""}Default ${defs[k]}, at most ${lim[k]}.` },
      el("span", { class: "opt-name" }, text, agentOnly ? el("small", {}, " LLM agent") : null),
      input, el("span", { class: "opt-def", "data-default-for": k }, `default ${defs[k]}`));
  });
  $("#options").replaceChildren(
    el("div", { class: "menu-sep" }, "Budgets for the next run"),
    ...rows,
    el("div", { class: "opt-note" }, "A run that reaches a budget stops and shows what it found (a partial result). "
      + "Replays keep the budgets they were recorded with."),
    el("button", { class: "mini", id: "options-reset", type: "button", on: { click: () => {
      optionValues = {}; saveOptions(); renderOptions(); } } }, "Back to defaults"));
  syncOptions();
}
/** The button shows how many options differ from the defaults; changed rows are marked. */
function syncOptions() {
  const n = Object.keys(optionValues).length;
  $("#options-count").hidden = !n;
  $("#options-count").textContent = n ? String(n) : "";
  $("#options-btn").classList.toggle("changed", n > 0);
  for (const input of document.querySelectorAll("#options input[data-option]")) {
    input.closest(".opt-row").classList.toggle("changed", input.dataset.option in optionValues);
  }
}
/** The request fields for the chosen options (only what differs from the defaults). */
function optionRequest() {
  const budgets = Object.fromEntries(Object.entries(optionValues).filter(([k]) => k !== "max_agent_turns"));
  return { ...(Object.keys(budgets).length ? { budgets } : {}),
           ...("max_agent_turns" in optionValues ? { max_turns: optionValues.max_agent_turns } : {}) };
}
dropdown($("#options-btn"), $("#options"));
renderOptions();

// Mode: a two-way switch. It also follows the run on screen (a replay of an agent run shows "AI agent").
function getMode() { return $("#mode .on")?.dataset.mode || "deterministic"; }
function setMode(mode) {
  const b = $(`#mode [data-mode="${mode}"]`);
  if (!b) return;
  for (const x of document.querySelectorAll("#mode button")) {
    x.classList.toggle("on", x === b); x.setAttribute("aria-checked", String(x === b));
  }
}
for (const b of document.querySelectorAll("#mode button")) b.addEventListener("click", () => setMode(b.dataset.mode));
renderGroups(GROUPS);

// ---- startup ----------------------------------------------------------------------------------
async function refreshHealth() {
  try {
    health = await api.health();
    graph.maxVisible = health.ui?.max_visible_nodes || graph.maxVisible;
    if (!optionsFromServer && health.limits) { optionsFromServer = true; renderOptions(); }  // once, like the groups
    if (!groupsFromPolicy && Array.isArray(health.groups) && health.groups.length) {
      groupsFromPolicy = true;  // once: later polls must not reset the presenter's toggles
      renderGroups(health.groups.map((g) => [g.id, g.label, g.enabled_by_default, `${g.label} / ${g.label_es}`]));
    }
    const prov = health.provider ? `${health.provider.name}${health.provider.snapshot ? ` · snapshot ${health.provider.snapshot}` : ""}` : "no people graph installed (replay only)";
    $("#conn").className = "sd";
    $("#conn-text").textContent = "Server online";
    $("#provider").textContent = prov;
    const agentBtn = $('#mode [data-mode="agent"]'), detBtn = $('#mode [data-mode="deterministic"]');
    agentBtn.disabled = !health.agent.available;
    agentBtn.title = health.agent.available ? `LLM agent (${health.agent.model}): the model chooses every tool call`
      : "LLM agent unavailable: no API key on the server";
    detBtn.disabled = !health.provider;
    detBtn.title = "Deterministic baseline: a fixed frontier-ranking rule chooses every expansion (no LLM)";
    if (!health.agent.available && getMode() === "agent" && !model.id) setMode("deterministic");
  } catch {
    $("#conn").className = "sd err";
    $("#conn-text").textContent = "Server offline";
  }
}

async function loadCases() {
  try {
    const { cases } = await api.demoCases();
    const sel = $("#replay");
    // Grouped by who explored, so the titles can stay short ("(AI agent)" is said by the group).
    const recorded = cases.filter((c) => c.trace);
    const isAgent = (c) => c.mode === "agent" || /\(AI agent\)\s*$/.test(c.title || "");
    const opt = (c) => el("option", { value: c.id }, String(c.title || c.id).replace(/\s*\(AI agent\)\s*$/, ""));
    const group = (label, list) => list.length ? el("optgroup", { label }, ...list.map(opt)) : null;
    sel.replaceChildren(el("option", { value: "" }, recorded.length ? "Demo replays…" : "No recorded demo cases yet"),
      group("LLM agent", recorded.filter(isAgent)), group("Deterministic baseline (no LLM)", recorded.filter((c) => !isAgent(c))));
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
  if (q.get("mode")) setMode(q.get("mode"));
})();

// ---- run control ------------------------------------------------------------------------------
// One button: Explore when idle, Stop while a run is open (it used to be a separate Cancel button).
$("#go").addEventListener("click", async () => {
  if (model.id && model.status === "running") { try { await api.cancel(model.id); } catch {} return; }
  start();
});
$("#replay").addEventListener("change", () => { if ($("#replay").value) start($("#replay").value); });
$("#reset").addEventListener("click", () => {
  // A stepped run stays "running" until its last step is shown: stop it on the server too, then
  // re-enable Explore and the replay picker (they are disabled while a run is open).
  if (model.id && model.status === "running") api.cancel(model.id).catch(() => {});
  resetAll(); pickSource.clear(); pickTarget.clear();
  setRunning(false);
  setStatus("idle", "Pick two people and press Explore, or choose a replay.");
});

function resetAll() {
  es?.close(); es = null;
  model.reset(); graph.reset(); timeline.clear();
  book.reset(); stepRun = false; stepIndex = -1; $("#step-card").hidden = true; saveState = null;
  sandbox = null; pathMode = "strongest"; turtleStale = false;
  paths.update({ comparison: null, sandbox: null, answer: null, status: "idle" });
  editor.setText("", []);
  $("#inspector").hidden = true;
  setBadges(null);
  updateCounts();
}

async function start(replayCase) {
  let body;
  if (replayCase) body = { mode: "replay", case_id: replayCase, replay_delay_seconds: 0 };
  else {
    if (!pickSource.value || !pickTarget.value) { setStatus("error", "Pick both people from the suggestions first."); return; }
    const groups = enabledGroups();
    if (!groups.length) { setStatus("error", "Enable at least one relation group."); return; }
    body = { source_qid: pickSource.value.qid, target_qid: pickTarget.value.qid, mode: getMode(), groups, ...optionRequest() };
    if (body.mode !== "agent") delete body.max_turns;  // model turns only apply to the LLM agent
  }
  setStatus("busy", replayCase ? "Loading replay…" : "Starting…");
  try {
    const inv = await api.start(body);
    // Only clear the screen once the server has accepted the new run: a failed start (server down,
    // agent unavailable, bad input) must leave the previous result visible for the presenter.
    resetAll();
    model.id = inv.investigation_id;
    stepRun = true;
    renderStepCard();
    setRunning(true);
    es = streamEvents(inv.investigation_id, onEvent, () => { if (model.status === "running") setStatus("error", "Event stream lost; the server may be offline."); });
  } catch (err) {
    setStatus("error", `Could not start: ${err.message}`);
  } finally {
    $("#replay").value = "";
  }
}

function setRunning(on) {
  // While a run is open the switch shows that run's strategy and the menu its setup: lock both.
  $("#mode").classList.toggle("locked", on);
  $("#mode").title = on ? "The strategy of the run on screen (Reset or wait for the end to change it)" : "Who decides what to explore next";
  $("#groups-btn").disabled = on;
  $("#options-btn").disabled = on;
  $("#go").textContent = on ? "Stop" : "Explore";
  $("#go").classList.toggle("primary", !on);
  $("#go").title = on ? "Stop this run (what was found so far stays on screen)" : "Start exploring between the two people";
  $("#replay").disabled = on || !(window.__demoCases || []).some((c) => c.trace);
  $("#apply-sandbox").disabled = !editor.isDirty || !canEditSandbox();
  $("#clear-sandbox").disabled = !sandbox?.sandbox_edges?.length || on;
}

// ---- events -------------------------------------------------------------------------------------
function onEvent(ev) {
  if (stepRun) {
    if (book.push(ev)) {
      // Run over: the search for a stronger path that found none becomes one step (never a step the
      // presenter has already seen).
      if (book.done) book.foldTail(Math.max(stepIndex, 0));
      if (stepIndex < 0 && book.isComplete(0)) nextStep();  // the setup step shows on its own
      else renderStepCard();
    }
    return;
  }
  applyEvent(ev);
}

function applyEvent(ev) {
  const change = model.apply(ev);
  if (change.type === "duplicate") return;
  const p = ev.payload;
  switch (ev.type) {
    case "investigation.started":
      setBadges(p);
      setMode(p.strategy === "agent" ? "agent" : "deterministic");  // the switch shows the run on screen
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
      graph.addNodes(change.addedNodes.map((id) => model.nodes.get(id)), { origin: p.expanded_qid });
      graph.addEdges(change.addedEdges.map((id) => model.edges.get(id)), { fresh: !quietApply });
      graph.applyCap();
      scheduleTurtle();
      break;
    case "paths.updated":
      if (stepRun) graph.revealPaths(model.comparison);  // drawn hop by hop: the methodical style
      else graph.setPaths(model.comparison);
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
      + `(${String(f.stop_reason).replaceAll("_", " ")}) · within the explored subgraph only`);
  }
  if (!stepRun) graph.fitWhenSettled(model.comparison?.shortest?.result ? model.pathNodeIds() : null);  // stepping frames itself
  loadTurtle();
}

// ---- step-through ---------------------------------------------------------------------------------
function nextStep() {
  if (!stepRun || stepIndex >= book.lastComplete) return;
  stepIndex += 1;
  // The folded "looked further, nothing stronger" step adds its entities greyed out: bright green
  // would say "look here!" while the card says it led nowhere.
  quietApply = book.steps[stepIndex].kind === "tail";
  try { for (const ev of book.steps[stepIndex].events) applyEvent(ev); } finally { quietApply = false; }
  afterStep();
}


/** Back one step: re-fold the model up to that step and trim the views (no network rebuild). */
function backStep() {
  if (!stepRun || stepIndex <= 0) return;
  refoldTo(stepIndex - 1);
  afterStep();
}

/** Unfold the "looked for a stronger path" step and walk into its first call. */
function unfoldTail() {
  if (!book.folded || book.folded !== book.steps[stepIndex]) return;
  const i = book.unfoldTail();
  refoldTo(i - 1);
  nextStep();
}

/** Show the state right after step i: model, graph, timeline and path panel (no network rebuild). */
function refoldTo(i) {
  stepIndex = i;
  model.reset();
  for (const s of book.steps.slice(0, stepIndex + 1)) for (const ev of s.events) model.apply(ev);
  graph.retainOnly(new Set(model.nodes.keys()), new Set(model.edges.keys()), model.expanded);
  graph.setPaths(model.comparison);
  timeline.renderAll(model.timeline);
  paths.update({ comparison: model.comparison, answer: model.final?.answer || null,
                 status: model.final ? model.status : "running" });
  if (!model.final) setRunning(true);
  updateCounts();
  scheduleTurtle();
}

function afterStep() {
  const step = book.steps[stepIndex];
  const d = describeStep(step);
  // Story view: the trail (people, expanded nodes in order, paths) stays in colour, this step's new
  // nodes and edges are green, everything fetched earlier that led nowhere is greyed out.
  graph.focus = null;
  const now = step.kind !== "result";
  // The turn's planned calls: their target nodes and the branches to them show from the decision on.
  const turn = step.kind === "decision" ? stepIndex : step.kind === "call" ? step.parent ?? null : null;
  const planned = turn === null ? [] : book.plan(turn).map((t) => t.qid).filter(Boolean);
  const live = now && step.kind !== "tail";  // the tail's arrivals stay greyed: they led nowhere better
  graph.setPreviousPath(d.pathEvent === "improved" && d.prevPath ? d.prevPath.hops.map((h) => h.edge_id) : []);
  graph.setStory({ order: [...model.expanded], current: live ? d.nodeIds : [], currentEdges: live ? d.edgeIds : [],
                   active: live ? d.expandedQids : [], planned, calm: !!d.pathEvent });
  timeline.markCurrent(step.events.map((e) => e.event_id));
  renderStepCard();
  // Camera: frame the trail plus what this step added, beside the step card (a column at the top left),
  // or below it when it is minimized to its title row.
  const inset = cardInset();
  // Once a path exists its nodes hold still, so the frame below stays on it (leaves would drift away).
  if (step.kind === "result" || d.pathEvent) graph.pinNodes(model.pathNodeIds());
  // A path moment lays the path out as a line between the two people (an arc when it replaces an
  // earlier path, whose dashed line stays straight), so on screen it reads as the chain on the card.
  if (d.pathEvent && d.pathResult?.hops?.length) {
    const prior = book.steps.slice(0, stepIndex).filter((s) => s.pathEvent).length;
    const r = d.pathResult;
    graph.straightenPath([r.hops[0].from_qid, ...r.hops.map((h) => h.to_qid)], { bow: prior ? (prior % 2 ? -170 : 170) : 0 });
  }
  const focus = cameraFocus(step, d, planned);
  if (focus.center.size || focus.around.size) graph.frameStory(focus, inset);
  // Tools that do not touch the graph get an effect that depicts what they do.
  graph.stopFx();
  const tool = step.kind === "call" ? d.tools[0]?.tool : null;
  // Both follow the backend's own rules (hub cap, hop limit) so only what is really computed moves.
  const rules = { isHub: isHubNode, maxHops: model.meta?.max_hops ?? 6 };
  if (tool === "compute_paths" || tool === "inspect_path") graph.fxPathSearch(rules);
  else if (tool === "get_search_status") graph.fxFrontierScan(rules, d.tools[0]?.suggestions ?? null);
  if (step.kind !== "result") {
    // The card already says which step this is; the status bar only adds the keys.
    setStatus("busy", "Keys: → or Space = next step · ← = back");
  }
}

/** The part of the graph the step card covers, as the camera's {top, left} inset in pixels. */
function cardInset() {
  const card = $("#step-card");
  if (card.hidden) return { top: 0, left: 0 };
  const c = card.getBoundingClientRect(), n = $("#network").getBoundingClientRect();
  return card.classList.contains("min") ? { top: c.bottom - n.top + 8, left: 0 } : { top: 0, left: c.right - n.left + 8 };
}

function renderStepCard() {
  const card = $("#step-card");
  card.hidden = !stepRun;
  if (!stepRun) return;
  const waiting = stepIndex >= book.lastComplete && !book.done;
  $("#step-back").disabled = stepIndex <= 0;
  $("#step-next").disabled = stepIndex >= book.lastComplete;
  $("#step-next").textContent = waiting && stepIndex >= 0 ? "Waiting…" : "Next ▶";
  const total = Math.max(1, book.count - 1);
  $("#step-progress i").style.width = `${Math.max(0, Math.min(100, (100 * stepIndex) / total))}%`;
  if (stepIndex < 0) {
    $("#step-title").textContent = "Step-through";
    $("#step-sub").textContent = "waiting for the run to start…";
    $("#step-body").replaceChildren();
    return;
  }
  const step = book.steps[stepIndex];
  $("#step-title").textContent = stepIndex === 0 ? "Start" : `Step ${stepIndex} / ${book.count - 1}${book.done ? "" : "+"}`;
  $("#step-sub").textContent = waiting ? (model.isAgent ? "the agent is still working…" : "the search is still running…")
    : book.done || stepIndex >= book.lastComplete ? "" : `${book.lastComplete - stepIndex} more ready`;
  const body = $("#step-body");
  body.replaceChildren(...stepBody(step, describeStep(step)).flat().filter(Boolean));
  // A new step's lines ease in one after another; re-renders of the same step (live runs) do not replay it.
  if (body.dataset.step !== String(stepIndex)) {
    body.dataset.step = String(stepIndex);
    body.classList.remove("enter"); void body.offsetWidth; body.classList.add("enter");
  }
}

/**
 * What the camera frames for a step: it centres on the PARENT and zooms out just enough to watch its
 * tree grow. expansion: centre = the expanded node, around = its new branches (where they will end) ·
 * decision: centre = the trail node(s) the planned branches hang from, around = the planned nodes ·
 * a new/better path, the result: the path nodes · setup: the two people. Steps that change nothing in
 * the graph (compute_paths, get_search_status…) keep the camera still.
 */
function cameraFocus(step, d, planned) {
  const center = new Set(), around = new Set();
  const add = (set, ...xs) => xs.forEach((x) => x && set.add(x));
  if (step.kind === "setup") add(around, model.source?.id, model.target?.id);
  else if (step.kind === "decision" && !planned.some((q) => !model.expanded.has(q))) {
    add(around, ...graph.allVisibleIds());  // about the whole search (paths, status): show all of it
  } else if (step.kind === "decision") {
    for (const q of planned) {
      if (model.expanded.has(q)) continue;  // already on the trail: nothing new to show there
      add(around, q);
      for (const e of model.edges.values()) {
        if (e.subject === q && model.expanded.has(e.object)) add(center, e.object);
        if (e.object === q && model.expanded.has(e.subject)) add(center, e.subject);
      }
    }
  } else if (d.tools[0]?.tool === "get_search_status" && d.tools[0].suggestions?.length) {
    // the ranking: both people and every suggestion the agent was shown
    add(around, model.source?.id, model.target?.id, ...d.tools[0].suggestions.map((g) => g.qid));
  } else if (step.kind === "result") add(around, ...model.pathNodeIds());
  else if (step.kind === "tail") add(around, ...model.pathNodeIds(), ...d.expandedQids);  // the path and where it looked
  else if (d.pathEvent) add(around, ...model.pathNodeIds(), ...d.expandedQids, ...(d.prevPath?.nodes || []).map((n) => n.qid));
  else if (d.nodeIds.length || d.edgeIds.length) { add(center, ...d.expandedQids); add(around, ...d.nodeIds); }
  return { center, around };
}

/** The hub rule as the backend applies it (cap per node kind, from investigation.started). */
function isHubNode(n) {
  const m = model.meta || {};
  const cap = (m.max_intermediate_degree_by_kind || {})[n?.kind] ?? m.max_intermediate_degree;
  return !!n && n.kind !== "person" && cap != null && (n.degree || 0) > cap;
}

/** ① Beyoncé → ② Alice Braga → … (the last few expansions, in the order they happened). */
function trailLine() {
  const order = [...model.expanded];
  if (!order.length) return null;
  const first = Math.max(0, order.length - 7);
  const shown = order.slice(first).map((q, i) => `${orderBadge(first + i + 1)} ${label(model.nodes.get(q)) || q}`);
  return el("div", { class: "trail" }, el("span", { class: "muted" }, "Exploration order: "),
    first ? "… → " : "", shown.join(" → "));
}

/** "Diana, Princess of Wales's side" for "source": the people, not the search's jargon. */
function sideName(side) {
  const who = side === "source" ? model.source : side === "target" ? model.target : null;
  return who ? `${label(who)}'s side` : `${side} side`;
}

/**
 * Did this pick come from the tools? After a path exists: the improvement-lead list the agent was
 * handed (book.leadsFor); before: get_search_status's ranking (rankNote). Null when neither was recorded.
 */
function guidanceTag(qid, i) {
  const leads = qid ? book.leadsFor(i) : null;
  if (!leads) return rankNote(qid, i);
  const hit = leads.leads.find((l) => l.qid === qid);
  return hit
    ? el("span", { class: "rank-note hit" }, `lead #${hit.rank} from the tools: a stronger path could pass through it`)
    : el("span", { class: "rank-note miss" }, leads.leads.length
      ? `not on the tools' ${leads.leads.length} improvement leads: the agent's own pick`
      : "the tools listed no improvement leads: the agent's own pick");
}

/**
 * Honesty when the recording lacks what the tools showed the agent: without it the audience would
 * credit the model with picks that may have come straight from a tool's list.
 */
function guidanceNote(i) {
  const kind = book.unrecordedGuidance(i);
  if (kind === "leads") {
    return el("div", { class: "guidance" }, "Since a path exists, each tool result also hands the agent a list of improvement leads "
      + "(entities a stronger path could still pass through). This recording didn't store that list, so we can't show whether these picks came from it.");
  }
  if (kind === "ranking") {
    return el("div", { class: "guidance" }, "get_search_status handed the agent a ranked list just before this turn. "
      + "This recording didn't store it, so we can't show whether these picks followed it.");
  }
  return null;
}

/** Has an earlier step already called this tool? (Its plain-words description is shown once.) */
function toolSeenBefore(tool) {
  return book.steps.slice(0, stepIndex).some((s) => s.events.some((e) => e.type === "tool.started" && e.payload.tool === tool));
}

/**
 * The path moment, in words. First path: where the two people's sides met, and the chain. A stronger
 * path: why it beats the old one (from the hops' own strengths), then both chains, old one faded.
 */
function pathMoment(d) {
  if (!d.pathEvent || !d.pathResult) return null;
  const now = pathChain(d.pathResult);
  if (d.pathEvent === "first") {
    const m = meetingPoint(now, d.sides, d.expandedQids[0], d.edgeIds);
    return [
      el("div", { class: "star" }, m ? ["★ The two sides met at ", el("b", {}, m.meet.label), "!"] : "★ This connected the two sides!"),
      m ? el("div", { class: "res" }, `${sideName(m.already)} had already reached ${m.meet.label}; `,
        m.viaThisStep ? "this step found the link " : "the link is ", el("b", {}, `${m.from.label} —${m.rel.label}→ ${m.to.label}`), ".") : null,
      chainBlock(now, "Strongest path so far")];
  }
  const before = d.prevPath ? pathChain(d.prevPath) : null;
  return [
    el("div", { class: "star" }, "★ A stronger path replaced the old one."),
    before ? el("div", { class: "res" }, whyStronger(before, now)) : null,
    chainBlock(now, "Now"),
    before ? chainBlock(before, "Before (dashed in the graph)", true) : null];
}

/**
 * Where the backend ranked `qid` in the list the agent had just been shown before deciding step i
 * (book.rankingFor: an earlier turn's status check, graph unchanged since), e.g. "ranked #2 by the
 * backend" or "not in the backend's top 8 of 86"; null when there is no such list.
 */
function rankNote(qid, i) {
  const last = qid ? book.rankingFor(i) : null;
  if (!last) return null;
  const hit = last.suggestions.find((g) => g.qid === qid);
  const where = `step ${last.step}`;
  return hit
    ? el("span", { class: "rank-note hit" }, `ranked #${hit.rank} by get_search_status (${where})`)
    : el("span", { class: "rank-note miss" }, `not in get_search_status's top ${last.suggestions.length}`
      + `${last.total ? ` of ${last.total}` : ""} (${where}): the agent's own pick`);
}

/** The result of one tool call: the call's own counts, or the backend state right after it. */
function callResult(t, d) {
  if (t.error) return [el("div", { class: "res err" }, `✗ ${t.error.code}: ${t.error.message}`)];
  const lines = [];
  if (t.returned !== null) {
    if (!t.addedNodes && !t.addedEdges) {
      lines.push(el("div", { class: "res" }, `${t.returned} statements returned, all already in the explored subgraph: nothing new.`));
    } else {
      lines.push(el("div", { class: "res" }, `${t.returned} statements returned → `,
        el("b", { class: "fresh" }, `${t.addedNodes} new entities, ${t.addedEdges} new statements`), " (green in the graph)"));
    }
    if (t.truncated) {
      lines.push(el("div", { class: "res warn" },
        `Cut at ${t.returned}${t.capHit ? ` (${t.capHit.replaceAll("_", " ")})` : ""}: Wikidata has more statements for this entity.`));
    }
  } else if (t.tool === "compute_paths" || t.tool === "inspect_path") {
    const c = model.comparison;
    const w = c?.strongest?.result ? pathChain(c.strongest.result) : null;
    lines.push(el("div", { class: "res" }, w
      ? `Path found: ${w.hops} hop${w.hops === 1 ? "" : "s"}, ${strengthSummary(w)}.`
      : `No path yet in the explored subgraph (${model.nodes.size} entities, ${model.edges.size} statements).`));
  } else if (t.tool === "get_search_status") {
    const b = model.meta?.budgets || {};
    lines.push(el("div", { class: "res" }, `Explored subgraph: ${model.nodes.size} entities, ${model.edges.size} statements · `
      + `${model.expanded.size} of ${b.max_provider_requests ?? "?"} expansions used.`));
    if (t.suggestions?.length) {
      lines.push(el("div", { class: "res" }, `Tool result: the top ${t.suggestions.length}`
        + (t.frontierSize ? ` of ${t.frontierSize} frontier entities` : " frontier entities") + ", exactly as the agent saw them:"));
      lines.push(el("ol", { class: "ranks" }, ...t.suggestions.map((g) => el("li", {},
        el("span", { class: "rank-pill" }, `#${g.rank}`), " ", el("b", {}, g.label || g.qid),
        el("span", { class: "muted" }, ` · ${sideName(g.side)} · ${g.hops} hop${g.hops === 1 ? "" : "s"}`)))));
    } else if (t.ok) {
      lines.push(el("div", { class: "res" }, "Lit in green in the graph: the frontier, the explored entities the search could expand next."));
    }
  }
  return lines;
}

function callBlock(t, d) {
  const order = [...model.expanded];
  const rank = t.qid ? order.indexOf(t.qid) + 1 : 0;
  // Plain words first ("Expand Neymar"); the real tool name stays visible, small, for the curious.
  return el("div", { class: "call" },
    el("div", { class: "call-label" }, model.isAgent ? "Tool call and its result" : "Expansion and its result"),
    el("div", { class: "call-head" }, rank ? el("span", { class: "badge-n" }, orderBadge(rank)) : null,
      el("b", {}, toolAction(t)), t.side ? el("span", { class: "muted small" }, sideName(t.side)) : null,
      el("span", { class: "spacer" }), el("span", { class: "tool-name" }, t.tool)),
    // What the tool does is explained the first time it appears; after that the name is enough.
    toolSeenBefore(t.tool) ? null : el("div", { class: "purpose" }, toolPurpose(t, sideName)),
    callResult(t, d));
}

function stepBody(step, d) {
  const b = model.meta?.budgets || {};
  const toolRows = model.timeline.filter((r) => r.kind === "tool").length;
  const foot = budgetMeter([
    ["expansions", model.expanded.size, b.max_provider_requests],
    ["entities", model.nodes.size, b.max_explored_nodes],
    model.isAgent ? ["tool calls", toolRows, b.max_agent_tool_calls] : null,
  ]);
  const turnNo = (i) => book.steps.slice(0, i + 1).filter((s) => s.kind === "decision").length;
  switch (step.kind) {
    case "setup": {
      const m = model.meta || {};
      return [el("div", { class: "who" }, m.strategy === "agent" ? `LLM agent · ${m.model || "model"}` : "Deterministic baseline (no LLM)"),
        el("div", { class: "decision" }, `Find a connection between ${label(model.source)} and ${label(model.target)}.`),
        el("div", { class: "small muted" }, `Relation groups: ${(m.groups || []).join(", ")} · at most ${m.max_hops} hops`),
        el("div", { class: "small muted" }, "Where things sit on screen is only for readability: what connects two entities is a line (a statement), not how close they are drawn."),
        foot];
    }
    case "decision": {
      // The calls the model issued in this turn (all decided at once); each one is the next step.
      const plan = book.plan(stepIndex);
      const items = plan.length ? plan.map((t) => [el("b", {}, toolAction(t)), t.side ? el("span", { class: "muted small" }, ` · ${sideName(t.side)}`) : null,
          t.tool === "get_neighbors" ? [" ", guidanceTag(t.qid || t.arguments?.qid, stepIndex)] : null])
        : (step.events[0].payload.tool_calls || []).map((name) => [el("span", { class: "tool-name" }, name)]);
      return [el("div", { class: "who agent" }, `Agent turn ${turnNo(stepIndex)} · the model's message`),
        el("div", { class: "decision agent" }, d.decision ? `“${d.decision}”` : "(no message: the model went straight to its tool calls)"),
        items.length ? el("div", { class: "plan" },
          el("div", { class: "muted small" }, items.length === 1 ? "Its tool call:" : `Its ${items.length} tool calls (one step each):`),
          el("ol", {}, ...items.map((x) => el("li", {}, x)))) : null,
        guidanceNote(stepIndex), trailLine(), foot];
    }
    case "call": {
      const t = d.tools[0];
      const parent = step.parent ?? null;
      const why = parent !== null ? describeStep(book.steps[parent]).decision : null;
      // The agent's reasoning leads; what the backend did for it is the supporting box below.
      return [el("div", { class: "who agent" }, parent !== null
          ? `Agent turn ${turnNo(parent)} · tool call ${step.callNo} of ${book.callCount(stepIndex)}` : "Tool call"),
        why ? el("div", { class: "decision agent" }, `“${why}”`) : null,
        t?.tool === "get_neighbors" && guidanceTag(t.qid, stepIndex)
          ? el("div", { class: "small" }, guidanceTag(t.qid, stepIndex)) : null,
        t ? callBlock(t, d) : null,
        // On a path moment the path is the story; the exploration order stays in the graph's ①②③ badges.
        pathMoment(d), d.pathEvent ? null : trailLine(), foot];
    }
    case "expansion": {
      const t = d.tools[0];
      return [el("div", { class: "who" }, "Deterministic baseline (no LLM): expand the top-ranked frontier entity"),
        el("div", { class: "rule" }, d.rule || ""), t ? callBlock(t, d) : null, pathMoment(d), d.pathEvent ? null : trailLine(), foot];
    }
    case "tail": return tailBody(step, foot);
    default: {
      // The finale: the connection itself as a chain of people, works and organizations, then the
      // summary and an honest scope line. The full hop details stay in the History pane.
      const r = d.result || {};
      const h = resultHeadline(r);
      const chains = r.outcome === "path_found" ? pathChains(r.comparison || model.comparison) : [];
      const a = label(model.source), b = label(model.target);
      const plural = (n) => `a ${n}-hop path`;
      const headline = chains.length
        // "connected through", not "N apart": the strongest path may be longer than the shortest one.
        ? `${a} and ${b} are connected by ${plural(chains[0].hops)}.`
        : h.tone === "err" ? "The run failed before connecting the two people."
        : `No path connects ${a} and ${b} in the explored subgraph.`;
      return [el("div", { class: `who result ${h.tone}` }, h.head),
        el("div", { class: "decision" }, headline),
        ...chains.map((ch) => chainBlock(ch)),
        // Folded: the chain already shows the path, and a long summary would push the graph off screen.
        r.answer ? el("details", { class: "summary" }, el("summary", {}, r.answerSource === "agent"
          ? "The agent's final answer (grounding check passed)" : "Template answer from the verified path (no LLM)"), el("div", {}, r.answer)) : null,
        el("div", { class: "scope" }, h.scope), saveReplayBlock(r), foot];
    }
  }
}

// ---- save as demo replay ----------------------------------------------------------------------
// A finished LIVE run with a path can be kept as a demo case: the server re-checks it (same rules as
// scripts/investigate.py --record), writes its fixture and trace, and adds it to Demo replays.
// The block's state survives card re-renders (live runs re-render the card on every event).
let saveState = null;  // {open, busy, caseId, title, error, saved} for the current run

function saveReplayBlock(r) {
  if (!model.id || model.isReplay || r.outcome !== "path_found" || r.type === "investigation.failed") return null;
  const s = saveState ||= { open: false, busy: false, caseId: "", title: "", error: null, saved: null };
  if (s.saved) {
    return el("div", { class: "save-replay saved" }, `✓ Saved as demo replay “${s.saved.title}” (${s.saved.id}). `,
      el("span", { class: "muted" }, "It is now in Demo replays."));
  }
  if (!s.open) {
    return el("div", { class: "save-replay" }, el("button", { class: "mini", type: "button",
      title: "Keep this run as a recorded demo case (fixture + event trace + demo_cases.yaml entry)",
      on: { click: openSave } }, "Save as demo replay…"));
  }
  const field = (label, key, attrs) => el("label", { class: "save-field" }, el("span", { class: "muted small" }, label),
    el("input", { type: "text", value: s[key], spellcheck: "false", disabled: s.busy, ...attrs,
      on: { input: (e) => { s[key] = e.target.value; }, keydown: (e) => { if (e.key === "Enter") doSave(); e.stopPropagation(); } } }));
  return el("div", { class: "save-replay open" },
    el("div", { class: "save-title" }, "Save as demo replay"),
    field("Case id (lowercase, digits, hyphens)", "caseId", { maxlength: "64", pattern: "[a-z0-9-]+" }),
    field("Title shown in Demo replays", "title", { maxlength: "200" }),
    s.error ? el("div", { class: "res err" }, s.error) : null,
    el("div", { class: "save-actions" },
      el("button", { class: "btn primary", type: "button", disabled: s.busy, on: { click: doSave } }, s.busy ? "Checking…" : "Save"),
      el("button", { class: "btn", type: "button", disabled: s.busy, on: { click: () => { s.open = false; s.error = null; renderStepCard(); } } }, "Cancel"),
      el("span", { class: "muted small" }, "The server re-checks that the recording rebuilds this exact result before saving.")));
}

async function openSave() {
  const s = saveState;
  s.open = true; s.error = null;
  try {
    const sug = await api.recordSuggestion(model.id);
    s.caseId ||= sug.case_id; s.title ||= sug.title;
  } catch (err) { s.error = err.message; }
  renderStepCard();
  $("#step-card .save-field input")?.focus();
}

async function doSave() {
  const s = saveState;
  if (!s || s.busy) return;
  s.busy = true; s.error = null; renderStepCard();
  try {
    const res = await api.record(model.id, s.caseId.trim(), s.title.trim());
    s.saved = res.case; s.open = false;
    loadCases();  // the new case appears in Demo replays right away
  } catch (err) {
    s.error = err.message;
  } finally {
    s.busy = false;
    renderStepCard();
  }
}

/**
 * The folded tail: everything the run did after the strongest path last changed, in one card. Only
 * recorded facts: what was expanded, what it added, the agent's own reason, and that the path stayed.
 */
function tailBody(step, foot) {
  const t = describeTail(step, book.latestLeads(stepIndex - 1));
  const names = t.expansions.map((x) => label(model.nodes.get(x.qid)) || x.label);
  const shown = names.slice(0, 8).join(", ") + (names.length > 8 ? `, and ${names.length - 8} more` : "");
  const others = Object.entries(t.otherCalls).map(([tool, n]) => `${n}× ${tool}`).join(", ");
  const leadLine = t.fromLeads !== null && t.expansions.length
    ? el("div", { class: "res" }, `${t.fromLeads} of these ${t.expansions.length} expansions were on the tools' list of improvement leads`
      + (t.expansions.length - t.fromLeads ? `; ${t.expansions.length - t.fromLeads} were the agent's own picks.` : "."))
    : model.isAgent ? el("div", { class: "guidance" }, "Once a path exists, each tool result hands the agent a list of improvement leads "
      + "(entities a stronger path could still pass through). This recording didn't store that list, so we can't show how many of these picks came from it.")
    : null;
  return [
    el("div", { class: `who${model.isAgent ? " agent" : ""}` }, model.isAgent
      ? `The agent looks for a stronger path · ${t.steps} steps in one`
      : `Deterministic baseline (no LLM): a few more expansions to check for a stronger path · ${t.steps} steps in one`),
    el("div", { class: "decision" }, "Nothing stronger turned up: the path found at ",
      el("b", {}, `step ${step.pathStep}`), " is still the strongest."),
    t.reason ? el("div", { class: "why" }, el("span", { class: "muted" }, "The model's message when it kept going: "), `“${t.reason}”`) : null,
    t.expansions.length ? el("div", { class: "res" }, `Expanded ${t.expansions.length} more entit${t.expansions.length === 1 ? "y" : "ies"}: `, el("b", {}, shown), ".") : null,
    t.expansions.length ? el("div", { class: "res" }, `${t.addedNodes} new entities, ${t.addedEdges} new statements `
      + "(greyed out in the graph): none of them gave a stronger path.") : null,
    leadLine,
    others ? el("div", { class: "res muted small" }, `Also: ${others}.`) : null,
    el("button", { class: "mini unfold", type: "button", on: { click: unfoldTail } }, `Step through these ${t.steps} steps one by one`),
    foot];
}

/**
 * The budget as small meters: quiet grey while there is room, amber from 80%, red when used up (the
 * run stops there), so the presenter only notices it when it matters.
 */
function budgetMeter(rows) {
  return el("div", { class: "budget" }, rows.filter(Boolean).map(([name, used, max]) => {
    const f = max ? Math.min(1, used / max) : 0;
    const level = !max ? "" : f >= 1 ? " full" : f >= 0.8 ? " near" : "";
    return el("span", { class: `meter${level}`, title: `${name}: ${used} of ${max ?? "?"} allowed in this run` },
      el("span", { class: "m-name" }, name),
      el("span", { class: "m-bar" }, el("i", { style: `width:${Math.round(100 * f)}%` })),
      el("span", { class: "m-num" }, `${used}/${max ?? "?"}`));
  }));
}

/** One path as a chain: Neymar —acted in→ xXx … → Snoop Dogg, coloured like its path in the graph. */
function chainBlock(ch, title = "Strongest connection", faded = false) {
  const items = [];
  ch.nodes.forEach((n, i) => {
    if (i) {
      const rel = ch.rels[i - 1];
      items.push(el("span", { class: `rel ${rel.word || ""}${rel.sandbox ? " sandbox" : ""}`,
        title: `${rel.pid} · ${rel.group}${rel.word ? ` · ${rel.word} link (strength ${rel.strength})` : ""}` }, rel.label, " →"));
    }
    const chip = wikidataLink(n.qid, n.label);
    chip.className = `node k-${n.role || n.kind}`;
    chip.title = `${n.qid} · open in Wikidata`;
    items.push(chip);
  });
  return el("div", { class: `chain ${ch.mode}${faded ? " faded" : ""}`, title: `weighted cost ${ch.cost.toFixed(2)} (lower = stronger)` },
    el("div", { class: "chain-head" }, el("b", {}, title),
      el("span", { class: "muted" }, ` · ${ch.hops} hop${ch.hops === 1 ? "" : "s"}`)),
    el("div", { class: "chain-row" }, ...items),
    // The replaced path skips its strength line: the "why stronger" sentence already compares them.
    !faded && ch.rels.some((r) => r.word) ? el("div", { class: "chain-strength" }, strengthSummary(ch)) : null);
}

$("#step-next").addEventListener("click", nextStep);
// "−" / "+": minimize to the title row. It stays minimized across steps until reopened.
function setMinimized(box, button, min, what) {
  box.classList.toggle("min", min);
  button.textContent = min ? "+" : "−";
  button.title = `${min ? "Expand" : "Minimize"} ${what}`;
}
$("#step-min").addEventListener("click", () => {
  setMinimized($("#step-card"), $("#step-min"), !$("#step-card").classList.contains("min"), "this card (Back / Next stay available)");
});
$("#step-back").addEventListener("click", backStep);
document.addEventListener("keydown", (e) => {
  if (!stepRun || e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.target.closest?.("input, textarea, select, .CodeMirror")) return;  // typing in a picker or the editor
  if (["ArrowRight", "PageDown", " "].includes(e.key)) { e.preventDefault(); nextStep(); }
  else if (["ArrowLeft", "PageUp"].includes(e.key)) { e.preventDefault(); backStep(); }
});

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
    // While stepping, the server already holds later steps' edges: ask only for what is revealed.
    const ids = hiddenCount || stepRun ? graph.visibleEdgeIds() : null;
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
    return pick(c?.strongest);  // the demo shows the strongest path only
  })();
  editor.showGraphSelection({ edges: ids });
  if (ids.length) graph.fitTo(new Set(ids.flatMap((id) => { const e = graph.edges.get(id); return e ? [e.subject, e.object] : []; })));
}

function inspectorHead(text) {
  const box = $("#inspector");
  const min = el("button", { class: "mini min", type: "button" }, "−");
  setMinimized(box, min, box.classList.contains("min"), "the details");
  min.addEventListener("click", () => setMinimized(box, min, !box.classList.contains("min"), "the details"));
  return el("div", { class: "insp-head" }, el("span", { class: "grow" }, text), min,
    el("button", { class: "x", title: "Close", on: { click: () => { box.hidden = true; } } }, "×"));
}

function showInspector({ nodes, edges }) {
  const box = $("#inspector");
  box.hidden = false;
  if (edges.length && !nodes.length) {
    const e = graph.edges.get(edges[0]);
    if (!e) return;
    const s = model.nodes.get(e.subject), o = model.nodes.get(e.object);
    box.replaceChildren(
      inspectorHead(`${e.origin === "sandbox" ? "Sandbox edge (not Wikidata)" : "Wikidata statement"} · ${e.label}`),
      el("div", {}, wikidataLink(e.subject, label(s) || e.subject), " ",
        el("b", {}, "—", e.origin === "sandbox" ? e.label : wikidataLink(e.property_url, e.label), "→"), " ",
        wikidataLink(e.object, label(o) || e.object)),
      el("div", { class: "small muted mono" }, `${e.subject} ${e.origin === "sandbox" ? "sbx" : "wdt"}:${e.predicate} ${e.object}`),
      el("div", { class: "small muted" }, `group ${e.group || "?"} · source ${e.source} · edge ${e.id}`));
  } else {
    const n = model.nodes.get(nodes[0]);
    if (!n) return;
    const known = [...model.edges.values()].filter((e) => e.subject === n.id || e.object === n.id).length;
    box.replaceChildren(...[  // replaceChildren(null) would insert the text "null"
      inspectorHead(`${n.kind} · ${label(n) || n.id}`),
      el("div", {}, el("b", {}, label(n)), " ", wikidataLink(n.id, n.id)),
      n.label_es && n.label_es !== n.label_en ? el("div", { class: "small muted" }, `es: ${n.label_es}`) : null,
      el("div", { class: "small muted" }, `${n.degree} statements in the snapshot (degree) · ${known} in the explored subgraph`
        + `${model.expanded.has(n.id) ? " · expanded" : ""}`)].filter(Boolean));
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
  if (res.sandbox_edges.length) { paths.tab = "sandbox"; paths.render(); showPaths("strongest"); }
  else { paths.tab = "strongest"; paths.render(); showPaths(pathMode); }
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
  if (!p) { live.hidden = true; strat.hidden = true; return; }
  live.hidden = false;
  const replay = p.run_mode === "replay";
  live.className = `badge ${replay ? "replay" : "live"}`;
  live.textContent = replay ? `REPLAY · ${fmtTime(p.recorded_at)}` : "LIVE";
  live.title = replay ? `Recorded ${fmtTime(p.recorded_at)} and played back. Not a fresh exploration.`
    : "Running now against the local Wikidata snapshot.";
  strat.hidden = false;
  strat.className = `badge ${p.strategy === "agent" ? "agent" : "det"}`;
  strat.textContent = p.strategy === "agent" ? `${p.model || "LLM agent"}` : "baseline";
  strat.title = p.strategy === "agent" ? `Explored by the LLM agent (${p.model || "model"})` : "Explored by the deterministic baseline (no LLM)";
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

// ---- collapsible panes & drag --------------------------------------------------------------------
// Source (left) and History (right) collapse to a thin strip: the "−"/"+" in the pane bar or the
// strip itself toggle it.
function togglePane(cls, button, what) {
  document.body.classList.add("pane-anim");  // the pane slides (CSS --t-ui) instead of snapping
  const min = document.body.classList.toggle(cls);
  button.textContent = min ? "+" : "−";
  button.title = `${min ? "Expand" : "Collapse"} ${what}`;
  setTimeout(() => { document.body.classList.remove("pane-anim"); editor.refresh(); graph.network.redraw(); }, 480);
  return min;
}
function toggleSource(e) {
  e?.stopPropagation();
  togglePane("src-min", $("#src-min"), "the source pane");
}
function toggleSide(e) { e?.stopPropagation(); togglePane("side-min", $("#side-min"), "the history (result and timeline)"); }
$("#src-min").addEventListener("click", toggleSource);
$("#epane .pane-bar").addEventListener("click", () => { if (document.body.classList.contains("src-min")) toggleSource(); });
$("#side-min").addEventListener("click", toggleSide);
$("#paths-sec .pane-bar").addEventListener("click", () => { if (document.body.classList.contains("side-min")) toggleSide(); });
(() => {
  const drag = $("#drag"), pane = $("#epane");
  let x0 = 0, w0 = 0, on = false;
  drag.addEventListener("mousedown", (e) => { on = true; x0 = e.clientX; w0 = pane.offsetWidth; drag.classList.add("on"); e.preventDefault(); });
  window.addEventListener("mousemove", (e) => { if (on) pane.style.width = `${Math.max(220, Math.min(window.innerWidth * 0.6, w0 + e.clientX - x0))}px`; });
  window.addEventListener("mouseup", () => { if (on) { on = false; drag.classList.remove("on"); editor.refresh(); } });
})();

window.__workbench = { model, graph, editor, timeline, paths, book, nextStep, backStep,
                       get stepIndex() { return stepIndex; } };  // handy for debugging and E2E checks
