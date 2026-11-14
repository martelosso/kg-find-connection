// Workbench wiring: event stream -> model -> graph / timeline / paths / Turtle, plus the three
// highlight links (graph <-> Turtle, editor selection -> graph, timeline step -> fetched edges),
// path toggles, sandbox overlay, replay and status indicators.

import { api, streamEvents } from "./api.js";
import { $, el, wikidataLink } from "./dom.js";
import { GraphView } from "./graph-view.js";
import { InvestigationModel, label, pathsText } from "./investigation.js";
import { PathComparison } from "./path-comparison.js";
import { PeoplePicker } from "./people-picker.js";
import { StepBook, describeStep, orderBadge, toolPurpose, toolTarget } from "./steps.js";
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
// Step-through presentation (plan §2): every run is stepped. Its events are buffered in `book` and
// revealed one step at a time with Next / Back; nothing plays itself out.
let stepRun = false;         // a run is being stepped through
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
  book.reset(); stepRun = false; stepIndex = -1; $("#step-card").hidden = true;
  sandbox = null; pathMode = "both"; turtleStale = false;
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
  $("#go").disabled = on; $("#cancel").disabled = !on; $("#replay").disabled = on || !(window.__demoCases || []).some((c) => c.trace);
  $("#apply-sandbox").disabled = !editor.isDirty || !canEditSandbox();
  $("#clear-sandbox").disabled = !sandbox?.sandbox_edges?.length || on;
}

// ---- events -------------------------------------------------------------------------------------
function onEvent(ev) {
  if (stepRun) {
    if (book.push(ev)) {
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
      graph.addEdges(change.addedEdges.map((id) => model.edges.get(id)), { fresh: true });
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
      + `(${String(f.stop_reason).replaceAll("_", " ")}) · among explored relationships only`);
  }
  if (!stepRun) graph.fitWhenSettled(model.comparison?.shortest?.result ? model.pathNodeIds() : null);  // stepping frames itself
  loadTurtle();
}

// ---- step-through ---------------------------------------------------------------------------------
function nextStep() {
  if (!stepRun || stepIndex >= book.lastComplete) return;
  stepIndex += 1;
  for (const ev of book.steps[stepIndex].events) applyEvent(ev);
  afterStep();
}

/** Back one step: re-fold the model up to that step and trim the views (no network rebuild). */
function backStep() {
  if (!stepRun || stepIndex <= 0) return;
  stepIndex -= 1;
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
  afterStep();
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
  graph.setStory({ order: [...model.expanded], current: now ? d.nodeIds : [], currentEdges: now ? d.edgeIds : [],
                   active: now ? d.expandedQids : [], planned });
  timeline.markCurrent(step.events.map((e) => e.event_id));
  renderStepCard();
  // Camera: frame the trail plus what this step added, below the step card.
  const card = $("#step-card");
  const inset = card.hidden ? 0 : card.getBoundingClientRect().bottom - $("#network").getBoundingClientRect().top + 8;
  const focus = cameraFocus(step, d, planned);
  if (focus.center.size || focus.around.size) graph.frameStory(focus, inset);
  // Tools that do not touch the graph get an effect that depicts what they do.
  graph.stopFx();
  const tool = step.kind === "call" ? d.tools[0]?.tool : null;
  // Both follow the backend's own rules (hub cap, hop limit) so only what is really computed moves.
  const rules = { isHub: isHubNode, maxHops: model.meta?.max_hops ?? 6 };
  if (tool === "compute_paths" || tool === "inspect_path") graph.fxPathSearch(rules);
  else if (tool === "get_search_status") graph.fxFrontierScan(rules);
  if (step.kind !== "result") {
    setStatus("busy", `Step-through: ${stepIndex <= 0 ? "start" : `step ${stepIndex} of ${book.count - 1}${book.done ? "" : "+"}`}`
      + " · Next ▶ (→, Space, Page Down) · ◀ Back (←, Page Up)");
  }
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
  } else if (step.kind === "result") add(around, ...model.pathNodeIds());
  else if (d.pathEvent) add(around, ...model.pathNodeIds(), ...d.expandedQids);
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

/** The result of one tool call: the call's own counts, or the backend state right after it. */
function callResult(t, d) {
  if (t.error) return [el("div", { class: "res err" }, `✗ ${t.error.code}: ${t.error.message}`)];
  const lines = [];
  if (t.returned !== null) {
    if (!t.addedNodes && !t.addedEdges) {
      lines.push(el("div", { class: "res" }, `${t.returned} relationships returned, all already in the graph: nothing new.`));
    } else {
      lines.push(el("div", { class: "res" }, `${t.returned} relationships returned → `,
        el("b", { class: "fresh" }, `${t.addedNodes} new nodes, ${t.addedEdges} new edges`), " (green in the graph)"));
    }
    if (t.truncated) {
      lines.push(el("div", { class: "res warn" },
        `Cut at ${t.returned}${t.capHit ? ` (${t.capHit.replaceAll("_", " ")})` : ""}: Wikidata has more for this node.`));
    }
  } else if (t.tool === "compute_paths" || t.tool === "inspect_path") {
    const c = model.comparison;
    lines.push(el("div", { class: "res" }, c?.shortest?.result || c?.strongest?.result
      ? `Connected: ${pathsText(c)}`
      : `No connection yet among the ${model.nodes.size} explored nodes and ${model.edges.size} edges.`));
  } else if (t.tool === "get_search_status") {
    const b = model.meta?.budgets || {};
    lines.push(el("div", { class: "res" }, `So far: ${model.nodes.size} nodes and ${model.edges.size} edges explored, `
      + `${model.expanded.size} of ${b.max_provider_requests ?? "?"} expansions used.`));
  }
  if (d.pathEvent) {
    lines.push(el("div", { class: "star" }, d.pathEvent === "first" ? "★ This connected the two sides! " : "★ A better path appeared: ",
      d.paths || ""));
  }
  return lines;
}

function callBlock(t, d) {
  const order = [...model.expanded];
  const rank = t.qid ? order.indexOf(t.qid) + 1 : 0;
  return el("div", { class: "call" },
    el("div", { class: "call-head" }, rank ? el("span", { class: "badge-n" }, orderBadge(rank)) : null,
      el("span", { class: "tool-name" }, t.tool), el("b", {}, toolTarget(t))),
    el("div", { class: "purpose" }, toolPurpose(t)),
    callResult(t, d));
}

function stepBody(step, d) {
  const b = model.meta?.budgets || {};
  const toolRows = model.timeline.filter((r) => r.kind === "tool").length;
  const foot = el("div", { class: "foot" }, [
    `requests ${model.expanded.size}/${b.max_provider_requests ?? "?"}`,
    `nodes ${model.nodes.size}/${b.max_explored_nodes ?? "?"}`,
    model.isAgent ? `tool calls ${toolRows}/${b.max_agent_tool_calls ?? "?"}` : null,
  ].filter(Boolean).join(" · "));
  const turnNo = (i) => book.steps.slice(0, i + 1).filter((s) => s.kind === "decision").length;
  switch (step.kind) {
    case "setup": {
      const m = model.meta || {};
      return [el("div", { class: "who" }, m.strategy === "agent" ? `AI agent · ${m.model || "model"}` : "Deterministic exploration (no AI)"),
        el("div", { class: "decision" }, `Find a connection between ${label(model.source)} and ${label(model.target)}.`),
        el("div", { class: "small muted" }, `Relationship groups: ${(m.groups || []).join(", ")} · at most ${m.max_hops} hops`), foot];
    }
    case "decision": {
      // The calls the model issued in this turn (all decided at once); each one is the next step.
      const plan = book.plan(stepIndex);
      const items = plan.length ? plan.map((t) => [el("span", { class: "tool-name" }, t.tool), " ", toolTarget(t)])
        : (step.events[0].payload.tool_calls || []).map((name) => [el("span", { class: "tool-name" }, name)]);
      return [el("div", { class: "who agent" }, `The agent decides · turn ${turnNo(stepIndex)}`),
        el("div", { class: "decision agent" }, d.decision ? `“${d.decision}”` : "(no text: the model called tools without writing a decision)"),
        items.length ? el("div", { class: "plan" },
          el("div", { class: "muted small" }, items.length === 1 ? "Its next call:" : `Its next ${items.length} calls, one per step:`),
          el("ol", {}, ...items.map((x) => el("li", {}, x)))) : null,
        trailLine(), foot];
    }
    case "call": {
      const t = d.tools[0];
      const parent = step.parent ?? null;
      const why = parent !== null ? describeStep(book.steps[parent]).decision : null;
      return [el("div", { class: "who agent" }, parent !== null
          ? `Turn ${turnNo(parent)} · call ${step.callNo} of ${book.callCount(stepIndex)}` : "Tool call"),
        t ? callBlock(t, d) : null,
        why ? el("div", { class: "why" }, el("span", { class: "muted" }, "The agent's reason for this turn: "), `“${why}”`) : null,
        trailLine(), foot];
    }
    case "expansion": {
      const t = d.tools[0];
      return [el("div", { class: "who" }, "Backend rule (no AI): expand the best-ranked frontier node"),
        el("div", { class: "rule" }, d.rule || ""), t ? callBlock(t, d) : null, trailLine(), foot];
    }
    default: {
      const r = d.result || {};
      const found = r.outcome === "path_found";
      const head = r.type === "investigation.failed" ? "Failed" : r.type === "investigation.completed" ? "Completed" : "Partial result";
      return [el("div", { class: "who" }, head),
        el("div", { class: "decision" }, r.error && !found ? `${r.error.code}: ${r.error.message}`
          : found ? r.paths : "No connection among the explored relationships."),
        r.answer ? el("div", { class: "small" }, r.answer) : null,
        el("div", { class: "small muted" }, `stop: ${String(r.stop || "?").replaceAll("_", " ")} · among explored relationships only`),
        trailLine(), foot];
    }
  }
}

$("#step-next").addEventListener("click", nextStep);
$("#step-hide").addEventListener("click", () => { $("#step-card").hidden = true; });
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
    return [...(mode !== "strongest" ? pick(c?.shortest) : []), ...(mode !== "shortest" ? pick(c?.strongest) : [])];
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
      el("div", { class: "small muted" }, `${n.degree} links in the snapshot · ${known} in the explored graph`
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

// ---- collapsible panes & drag --------------------------------------------------------------------
// Source (left) and Result + timeline (right) collapse to a thin strip: the "−" in the pane bar, the
// strip itself and (for the source) the graph bar's "Source" button all toggle it.
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
  const min = togglePane("src-min", $("#src-min"), "the source pane");
  $("#toggle-src").classList.toggle("on", !min);
}
function toggleSide(e) { e?.stopPropagation(); togglePane("side-min", $("#side-min"), "the result and timeline"); }
$("#toggle-src").addEventListener("click", toggleSource);
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
