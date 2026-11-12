# Six Degrees of Knowledge — Implementation Plan

> **Audience:** Coding agent implementing the feature in the existing `knowledge-graph-workshop` repository.  
> **Status:** Implementation specification; Wikidata acquisition strategy intentionally remains a decision point.  
> **Goal:** An interactive, reliable workshop demo of **an AI agent navigating a knowledge graph** to connect two notable people, producing both a **shortest** and a **strongest** defensible path.

## 0. Read this first: objectives and engineering principles

Build **Six Degrees of Knowledge**, an agentic connection explorer. The user chooses two notable people. The system resolves their Wikidata identities, explores a relationship graph, computes candidate paths, and presents:

1. **Shortest:** fewest *eligible* edges among the relationships currently explored (or a certified result if the entire configured search space is known).
2. **Strongest:** lowest weighted cost over eligible edges, where meaningful direct relationships generally score higher than generic associations, with an explicit hop penalty.

**The agent chooses what to investigate next; code performs graph queries, pathfinding, ranking, and data validation.** The visual experience is essential: show which neighborhood the agent expanded, which edges were retrieved, which paths were considered, and which supporting Turtle triples correspond to the selected graph edges.

**Non-goals for the first version:** proof that two people met; Wikipedia scraping; arbitrary SPARQL generation by an LLM; a multiagent framework; full Wikidata ingestion; automatic ontology changes; a universal optimal path across all Wikidata; a general-purpose chat assistant; a graph database deployment.

**Rules for honest outcomes:**
- A Wikidata assertion is evidence that Wikidata records that statement, **not** independently verified historical truth.
- A shared employer, school, organization, or event is **not** proof of direct personal contact.
- Absence of an edge is **unknown**, not proof of no relationship.
- A path found in a cached or partially explored graph is not guaranteed to be globally shortest/strongest. The UI MUST say *“shortest/strongest among explored relationships”* unless completeness has actually been established.
- Never fabricate people, links, property IDs, provenance, or scores. Failed/unavailable searches should produce an honest incomplete result.

---

## 1. Existing repository assessment (reviewed from the supplied ZIP)

The supplied repository is a small Python 3.13+ / `uv` project, with Flask, RDFLib, an OpenAI-compatible OpenRouter client, and two separate HTML frontends.

| Existing path | What it contains | Recommendation |
|---|---|---|
| `graph-view/live-editor.html` | **Best asset:** CodeMirror 5 editor, vis-network graph, browser Turtle parser, syntax highlighting, editable Turtle, `.ttl` open/save, schema/instance filters, graph↔source highlighting with character offsets | **Retain as foundation for main workshop UI.** Refactor JS/CSS into modules as needed; preserve interaction behavior. Serve via Flask rather than opening `file://`. |
| `graph-view/vis-network.min.js` | Vendored graph visualization library | Reuse; no need for a new graph library in UI for MVP. |
| `graph-view/visualize.html` | Older standalone visualizer | Keep as legacy demo or retire after feature parity; do not evolve both. |
| `src/ui/index.html` | Separate retail-focused chat + ontology graph UI | Treat as **legacy**; do not build the new product on its old layout. Salvage any useful rendering/trace ideas only. |
| `src/server.py` | Flask server with `/`, `/api/ontology`, `/api/ask`; currently serves retail UI | Reuse Flask application foundation, **replace/extend routes**, and serve new live-editor workspace as `/`. |
| `src/ontology_agent.py` | OpenRouter client, model/tool loop, `sparql_query` tool, RDFLib graph caching, steps for UI | Reuse **client setup and generic tool-call orchestration ideas**, but **separate domain-specific logic**. Retail prompt, regex SPARQL introspection, single global data graph and Spanish traversal summaries are not appropriate for new domain. |
| `ontology/ontology.owl.ttl` | Retail / supermarket ontology | Preserve as teaching/legacy asset, **do not reuse as new people-domain ontology**. |
| `ontology/your_own_ontology.owl.ttl` | Scratch ontology | Leave untouched. |
| `data/mock_data.ttl` | Retail instances | Preserve as example; introduce new namespaced data directories. |
| `pyproject.toml`, `uv.lock` | Python project metadata | Continue using `uv`; add only necessary dependencies. |
| tests | No tests in ZIP | Add test suite from first milestone. |

### Specific limitations to address

- `live-editor.html` contains a **custom, partial Turtle parser**. It is good for controlled demo syntax, but it must not be assumed to parse arbitrary Wikidata RDF/Turtle correctly (blank nodes, qualifiers, quoted literals, etc.).
- Source-offset highlighting is calculated in the browser for the parsed editor text and becomes stale after edits. Preserve the safety check and provide a controlled bridge from backend triple IDs → regenerated Turtle source spans.
- The existing `/api/ask` returns results at the end of the tool loop; live navigation needs **incremental events** rather than only a final transcript.
- The old backend currently uses a module-global RDFLib cache for one static file; replace with per-investigation state plus a reusable read-only/shared cache to prevent cross-session leakage.
- UI HTML is monolithic and two alternative UIs exist; refactor for maintainability, without creating an unnecessary frontend framework migration.

### Migration and compatibility

- Keep the old retail assets available under `examples/retail/` or clearly mark them as legacy. Prefer moving them only when imports/references are updated and verified.
- New default app: the explorer. Old terminal retail assistant may remain an example but should not be imported into the production request path.
- Preserve *editor-selected text → graph highlighting* and *graph selection → highlighted Turtle source*. Add **agent step → graph edge IDs → corresponding source span** as the third interaction.

---

## 2. User experience and workshop flow

### Main screen

1. **Top bar / search form:** two person selectors with typeahead, Wikidata label/description, disambiguation, person image if available (optional), **Explore** and **Reset**.
2. **Graph Explorer (primary, largest area):** progressive graph growth, source and target pinned/distinguished, new edges briefly emphasized, alternate paths de-emphasized, stable selection and legible labels; pan/zoom/fit/focus.
3. **Investigation timeline:** human-readable live tool activity: resolve persons → expand neighbors → find candidates → validate/compare → result. Click a step to highlight added nodes/edges. Include a concise agent *decision summary*, not hidden chain of thought.
4. **Result comparison:** Shortest and Strongest tabs/cards with ordered hops, relationship labels, number of edges, score/cost where appropriate, Wikidata links, and a clear explored-graph qualification. Highlight either path.
5. **RDF Inspector/editor:** preserve existing two-way CodeMirror↔graph highlighting. Display **a compact, canonical Turtle projection of the currently visible/selected triples**, rather than enormous raw Wikidata entity dumps. Let the presenter edit a **sandbox overlay** and see how a path changes (separate from authoritative Wikidata source records).
6. **Status/fallback:** pending, exploring, partial result, completed, no path within bounds, rate-limited/offline, and agent service unavailable. No fabricated success state.

### Live demo sequence (roughly 8–12 minutes of live activity)

- Choose a **pretested** pair of notable people; start graph almost empty (only endpoints).
- Show the agent expanding from both ends; explain typed predicates and what one triple represents.
- A connection appears. Show **Shortest** and **Strongest**; they may coincide, and this must be supported rather than artificially forced.
- Click an intermediate relationship; graph highlights corresponding Turtle statement and a link to the Wikidata entity/property.
- Select a tool event to show which edges it fetched and why.
- Optional: enable a **weak-association** relation category or a sandbox edit, recalculate, and observe the ranking change.
- Conclude with the enterprise analogy: beneficial ownership investigations or cross-domain entity discovery. Do **not** build a second enterprise demo into MVP.

### Presenter safeguards

- Provide **Replay** mode using captured authentic events and frozen cached data; same UI as a live run.
- Prepare at least **3 prevalidated pairs**, stored as QIDs and display labels in a fixture/config. At least one should illustrate different candidate paths **if real data supports it**.
- Add a visible **Live / Replay** mode indicator. Never misrepresent replay as fresh live Wikidata exploration.
- Run without network for the workshop using the fixture snapshots. Network/API failures must not break the presentation.

---

## 3. Data semantics and graph design

### 3.1 Core identity and entities

Use **canonical Wikidata QIDs** (`Q…`) for people and related entities. Use Wikidata property IDs (`P…`) for predicates. Maintain human-readable labels separately.

Graph node: `id`, `wikidata_url`, `label`, `description?`, `kind` (`person`, `organization`, `institution`, `event`, `other`), `source`, `fetched_at`.

Graph edge: stable `edge_id`, `subject_qid`, `property_pid`, `object_qid`, `direction`, `relation_group`, `base_strength`, `source_statement_id?`, `qualifiers?`, `retrieved_at`, `origin` (`wikidata`, `sandbox`). Include original Wikidata entity/property URLs.

Avoid reducing everything to generic `CONNECTED_TO`. The **semantics of each relationship** are the point of the workshop.

### 3.2 Relationship policy: start small

Create one versioned `config/relationships.yaml` with **explicit eligible Wikidata properties**, display labels, strengths/costs, traversal direction, and reverse semantics. Start by validating the following candidates against actual data and Wikidata property definitions:

- interpersonal: `P26` spouse, `P22` father, `P25` mother, `P3373` sibling, `P1066` student of; possibly other verified interpersonal properties.
- institutional: `P108` employer, `P69` educated at, `P463` member of, `P39` position held (the latter often points to a role rather than a person, so use sparingly).

**Important:** A `person → employer → person` edge is invalid. The underlying path is `person → employer/institution ← person`. Show that organization as a distinct graph node and count actual traversed edges, or create an explicitly labeled **derived association** with preserved source links and deterministic scoring. The simpler MVP is to preserve the intermediate organization node.

- Maintain a conservative **allowlist**, not every Wikidata property.
- Relationship directions and inverses must be explicit (e.g., spouse can be traversed symmetrically for connectivity, while `student of` retains its original direction for display).
- Gate temporal/institutional derived links so they are **associations**, not claims that people personally interacted.
- Exclude metadata, categories, generic countries, and extremely high-degree hubs from eligible paths by default. Surface filters in config, not hardcoded scattered constants.
- Keep evidence and strength distinct: strength measures the *semantic relevance* of the relationship type, not confidence that a Wikidata claim is true.

### 3.3 Minimal local ontology

Create `ontology/people_connections.ttl` as a **small teaching ontology**, not a replacement for Wikidata's schema:

- `ex:Person`, `ex:Organization`, `ex:Connection`, `ex:Investigation`, `ex:Evidence` (only if actually used).
- Annotations describing relation groups and how the explorer interprets eligible predicates.
- `owl:equivalentProperty`, inverse or subproperty axioms **only where semantically defensible**. Avoid incorrectly equating weak shared affiliation with direct personal relationships.
- Wikidata original properties remain preserved in the graph. The local schema is a mapping/interpretation layer.

**Separate:** `ontology/people_connections.ttl` (semantics), `config/relationships.yaml` (ranking/search policy), `data/wikidata/…` (external facts), `data/sandbox/…` (presenter modifications). Ontology ≠ graph data ≠ score policy.

### 3.4 Turtle representation

Use RDFLib to produce **valid canonical Turtle** for the explorer's *selected/visible subset*. Include `wd:`, `wdt:`, `rdfs:`, and `ex:` prefixes as appropriate. Show labels with `rdfs:label` or in UI metadata.

The renderer must maintain a **stable correspondence** among backend triple identity, frontend edge ID, and source text spans. Since RDFLib's serializer does not guarantee layout or source positions, implement a deterministic **projection serializer** for the limited shown triples (or parse its own serialized Turtle into exact spans). The backend is the source of semantic validity; the browser may use a lightweight parser for highlighting/preview only.

For the demo, use Wikidata **truthy/direct statements** as the default projection. Qualifiers/references and statement-level provenance can be retained as metadata for later expansion; do not silently treat qualifier-less statements as complete historical assertions.

---

## 4. Wikidata RDF acquisition — deliberately unresolved design decision

**Do not commit to one ingestion mechanism until a small proof-of-concept compares them.** Implement a provider interface so alternatives can be swapped without rewriting the agent, graph model, or UI.

### Candidate approaches

| Approach | Advantages | Drawbacks / role |
|---|---|---|
| **A. Wikidata `Special:EntityData/QID.ttl`** | Native Turtle/RDF for specific entities; straightforward RDFLib parsing; `?flavor=simple` / `?flavor=dump` options | Entity-centric outgoing data; can be verbose; inverse edges and neighborhood expansion need separate treatment. |
| **B. Wikidata SPARQL Query Service (WDQS)** | Query precise outgoing/incoming relations, filtered predicates, labels, type constraints; easy neighborhood extraction | Public service limits and slow/expensive wide fan-out queries; not for bulk harvesting. |
| **C. Wikibase REST API / Action API** | Targeted entity lookup/search, labels/statements; structured JSON | Requires conversion to RDF/local edge model; incoming relationships not straightforward. |
| **D. Offline prepared Turtle snapshot** | Fast, stable, reproducible workshop | Needs pre-generation, refresh procedure, and explicit distinction from live queries. |

**Working architecture hypothesis, not a final decision:** an **offline Turtle snapshot** for reliable workshops + **targeted WDQS lookups** for controlled expansions + **REST/Action API for entity resolution**, with optional `Special:EntityData` for direct native Turtle ingestion. Validate feasibility first.

### Provider contract

```python
class KnowledgeProvider(Protocol):
    def search_people(self, text: str, limit: int = 8) -> list[PersonMatch]: ...
    def get_entities(self, qids: list[str]) -> list[EntityRecord]: ...
    def expand(
        self, qids: list[str],
        allowed_properties: set[str],
        direction: Literal["outgoing", "incoming", "both"],
        max_edges: int,
    ) -> ExpansionBatch: ...
```

`ExpansionBatch` must include edges, nodes, source URL(s), request metadata, pagination/truncation flags, and whether expansion is complete for the specified property set and direction. A provider can supply RDF directly or return normalized records to be serialized as RDF. Do **not** require the backend to use fetched `.ttl` files for every request if another path is measurably better.

### Proof-of-concept decision gate (first engineering spike)

1. Select 5–10 famous people with different relationship densities; resolve to QIDs.
2. Compare outgoing-only and inbound + outbound neighborhood retrieval under the property allowlist.
3. Check that person→institution←person joins are supported without huge unrestricted queries.
4. Measure response times, record size, coverage, truncation, cache hit rate, and how reliably candidate paths emerge for **three intended demo pairs**.
5. Verify raw RDF parsing and conversion to the compact visual projection; decide whether `Special:EntityData` is needed.
6. Document findings in `docs/wikidata-ingestion-decision.md` and choose the production provider/fallback. **Do not claim that a Wikidata endpoint returns RDF just because it returns SPARQL SELECT JSON.**

Official documentation:  
- https://www.wikidata.org/wiki/Wikidata:Data_access  
- https://www.wikidata.org/wiki/Wikidata:REST_API  
- https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual

### Operational requirements for live access

- Descriptive Wikimedia-compliant `User-Agent` with contact/project identifier, connect/read timeouts, and bounded concurrency.
- Exponential backoff honoring `429 Retry-After`; strict retry limit.
- Cache all fetched entities/neighborhoods to disk; version/snapshot metadata and timestamps.
- No unauthenticated bulk crawling, no fetching the entire Wikidata graph, no unbounded `SERVICE wikibase:label` expansion.
- Rate-limit and cap `max_nodes`, `max_edges`, `max_hops`, per-node fan-out, API requests, and wall-clock time per run.
- Use a recorded snapshot if the endpoint is unavailable. Keep external content as data, never as agent instructions.

---

## 5. Search and path ranking (deterministic engine)

### Graph storage

- **RDFLib** is authoritative for RDF parsing, serialization, and SPARQL over the local graph.
- **NetworkX** (suggested dependency) is an in-memory projection optimized for BFS/Dijkstra and UI graph updates. Build it from vetted RDF triples. Use a `MultiDiGraph` or equivalent when multiple predicates/parallel relationships connect the same entities.
- Support multiple investigation sessions; avoid a single mutable global `Graph` shared across users. Cache canonical facts separately from per-session selected/visible facts and sandbox edits.
- Stable node IDs = QIDs; stable edge IDs = deterministic hash of `(subject, predicate, object, statement_id/source)`; do not use volatile array positions.

### Shortest path

- Use bounded **bidirectional BFS** or NetworkX equivalent over eligible graph edges, with explicit traversal semantics.
- Prefer shortest *by number of eligible edges*; tie-break deterministically by lower weighted cost, then stable ID ordering.
- Store proof path as a sequence of **concrete original edges** with original predicate, source and direction.

### Strongest path

Define an explicit, understandable cost such as:

`edge_cost = -log(max(min_strength, strength)) + hop_penalty + hub_penalty(optional)`

- Costs must be nonnegative to use Dijkstra.
- The strength table is curated and versioned, with consistent semantics.
- A longer, semantically meaningful chain may outrank a short weak one.
- Penalize overlong paths; put a strict hop limit even when using Dijkstra (vanilla Dijkstra is not sufficient to enforce an arbitrary hop constraint — use a hop-aware state graph or bounded candidate-path algorithm).
- Institutional connections can be represented as two graph edges through an organization, with a conservative cost policy.
- Strongest means **minimum policy-defined cost**, not empirically validated interpersonal strength.
- Return the scoring breakdown per edge and the total score/cost for inspection.
- If scores produce the same path for shortest and strongest, display the same path honestly.

### Dynamic incomplete graph / optimality

This is a major correctness requirement: adding newly retrieved neighbors can improve either path.

- Each expansion must record *which entity × predicate set × direction* was searched, and whether complete or truncated.
- Path results include `scope: "explored_graph" | "complete_configured_scope"`; default to `explored_graph`.
- Search timeout, partial provider results, and fan-out truncation are visible in status and outcome.
- Never assert “globally shortest” after an arbitrary number of expansions. A certification method could be future work.
- For demo smoothness, prefer finding credible paths over proving a global optimum across Wikidata.

### Expected pure functions

```python
compute_shortest_path(graph, source_qid, target_qid, policy) -> PathResult | None
compute_strongest_path(graph, source_qid, target_qid, policy) -> PathResult | None
rank_frontier(graph, source_qid, target_qid, visited, policy) -> list[ExpansionCandidate]
serialize_visible_turtle(graph, visible_edge_ids) -> TurtleProjection
```

Unit-test independently of the LLM.

---

## 6. Agent design: minimal but genuinely adaptive

### One orchestrator agent

Use one tool-calling LLM, ideally through the existing OpenRouter/OpenAI-compatible client. **Do not** add LangGraph or multiple agents for MVP unless replay/resume complexity makes it demonstrably worthwhile.

Its responsibility: plan **which graph neighborhood to explore next**, in response to frontier coverage, connection opportunities, weak vs strong candidate paths, and remaining budget. It must never invent paths or compute weights from memory.

### Suggested tools (typed inputs/outputs)

```python
resolve_person(name: str) -> list[PersonMatch]
get_neighbors(qid: str, relation_groups: list[str], direction: str, limit: int) -> ExpansionBatch
get_search_status() -> SearchStatus
compute_paths(source_qid: str, target_qid: str) -> PathComparison
inspect_path(path_id: str) -> PathEvidence
```

Optional `expand_frontier(qids: list[str], ...)` for batching; the server should implement all query/pagination/budget rules. Avoid a free-form `run_any_sparql` tool initially. The agent may suggest an expansion only if budget and allowlist permit it.

### Tool loop

1. Resolve identities (or user selects an exact Wikidata candidate).
2. Initialize two search frontiers and an empty/bounded investigation graph.
3. Agent reviews concise status: explored node counts, current candidates, relation groups still unexplored, request budget.
4. Agent selects an allowed neighborhood query; backend executes it and emits new visual events.
5. Backend recomputes deterministic shortest/strongest candidates after each material expansion.
6. Agent decides to continue, adjust relationship group, inspect evidence, or finish.
7. **Backend verifies** final `PathResult` objects, attaches graph evidence, and produces structured answer. The LLM only writes concise plain-language summaries from verified results.
8. Stop at explicit budgets, no-progress threshold, or a justified “good enough” result; report incomplete search accurately.

For fairness, run a **deterministic bidirectional expansion baseline** on the same cache and budget. The agent is not assumed superior. Workshop narrative: algorithms are best for traversal; the agent adapts exploration when relevant data is incomplete.

### Prompt and resilience

- System prompt defines objective, strong/weak relationship distinctions, legal tool usage, bounded requests, stop policy, and prohibition on ungrounded links.
- No full ontology or large TTL dump in prompt; include concise relation-group documentation and search status.
- Tool call inputs validated with Pydantic; no arbitrary URLs/commands; allowlist QIDs/PIDs and finite budgets.
- All tool outputs have structured error codes and truncation indicators; errors don't become fabricated edges.
- If no API key, either disable live agent clearly or use a clearly labeled **deterministic exploration mode**, not a pretend AI execution.

---

## 7. API contracts, events, and state

Retain Flask for MVP. Prefer streaming **Server-Sent Events (SSE)** or equivalent for a one-directional backend→frontend agent progress stream (browser `EventSource` or streamed `fetch`). Stop/cancel can use a separate POST endpoint. Streaming transport is an implementation detail; event schema is mandatory.

### Suggested routes

```text
GET  /                             # new workshop workspace
GET  /api/health
GET  /api/people/search?q=...
POST /api/investigations           # {source_qid, target_qid, mode, budgets, provider_mode}
GET  /api/investigations/{id}
GET  /api/investigations/{id}/events
POST /api/investigations/{id}/cancel
GET  /api/investigations/{id}/graph
GET  /api/investigations/{id}/turtle?scope=visible|selected
GET  /api/investigations/{id}/paths
POST /api/investigations/{id}/overlay  # optional sandbox-only edits
GET  /api/demo-cases
```

### Common event envelope

```json
{
  "event_id": 12,
  "investigation_id": "...",
  "type": "graph.expanded",
  "timestamp": "...",
  "payload": {
    "added_nodes": [{"id": "Q...", "label": "..."}],
    "added_edges": [{"id": "edge-...", "subject": "Q...", "predicate": "P...", "object": "Q..."}],
    "highlight_edge_ids": ["edge-..."],
    "complete": false
  }
}
```

Other event types: `investigation.started`, `identity.resolved`, `agent.decision` (short *decision summary*), `tool.started`, `tool.completed`, `paths.updated`, `investigation.completed`, `investigation.partial`, `investigation.failed`. Replay uses the exact same event schema. Include monotonically increasing sequence IDs for ordering/reconnect.

### State isolation

`InvestigationState` contains: QIDs, current explored graph, frontier coverage, discovered candidate paths, tool budget, status, timestamps, event log, optional sandbox overlay, and cancellation flag. Implement in-memory per-process initially, with serialized replay fixtures and optional persistence; production multiworker distribution is out of scope.

---

## 8. Frontend migration / enhancements

**Main strategy:** upgrade `graph-view/live-editor.html` rather than recreating the editor from scratch.

Suggested incremental refactor:

```text
src/ui/
  index.html                 # new app shell
  css/workbench.css
  js/api.js                  # HTTP + event streaming
  js/graph-view.js           # vis-network adapter
  js/turtle-editor.js        # CodeMirror + bidirectional selection
  js/investigation.js        # state/event reducer
  js/path-comparison.js     # result rendering
  js/main.js
```

If moving static assets to Flask static hosting, copy/move `graph-view/vis-network.min.js` and ensure its license notices remain intact. Existing CodeMirror 5 is CDN-hosted; for **offline workshop reliability**, vendor or package the permitted JS/CSS resources or provide an offline fallback.

### Implementation requirements

- Preserve original theme/look-and-feel where feasible; update generic branding to **Six Degrees of Knowledge**.
- Build a progressive graph: apply `added_nodes`/`added_edges` deltas; **do not recreate `vis.Network` on each event** (preserve positions, selections and camera).
- Stable layouts and batched graph updates; initial source/target separated; cap visual node count, with filters for unrelated explored branches.
- Selection support: graph edge → exact Turtle statement; editor selection → corresponding visible graph elements; agent step → newly retrieved edge IDs; final path → all its edge IDs.
- Keep the existing stale-offset guard; reparse/rebuild mapping after any text edit.
- Distinguish Wikidata facts (read-only/source) from sandbox edits. A sandbox change must not silently overwrite cached Wikidata facts.
- Show human-friendly relationship names alongside PIDs; clicking an edge shows original subject, predicate, object and Wikidata links.
- Make paths prominent: tabs **Shortest** and **Strongest**; show hop count, score/cost, edge list, and “explored graph” label.
- Timeline rows should show tool, count returned, truncation/error status, and action highlights. Avoid displaying invisible private model reasoning.
- Provide **Reset**, **Replay**, **Live/Offline indicator**, and clear empty states.
- Responsive enough for a projector and common desktop widths; mobile perfection is non-goal.

### Browser Turtle parser safety

The original tokenizer is not a full RDF Turtle parser. Two acceptable implementation choices:

1. **Preferred for MVP:** Backend generates predictable compact Turtle with only supported constructs; backend RDFLib validates it, client parser maps spans for highlighting.
2. **More robust:** Integrate a standards-compliant JS Turtle parser (e.g., N3.js) with source-span mapping, after evaluating added complexity.

Do not feed arbitrary full Wikidata TTL exports into the existing hand-written parser and assume correct results.

---

## 9. Suggested code layout

Keep the repo lightweight; separate domain, data access, planning and UI:

```text
README.md
pyproject.toml
.env.example
config/
  relationships.yaml
  demo_cases.yaml
ontology/
  people_connections.ttl
  ontology.owl.ttl              # legacy retail, keep or move
src/
  server.py                     # Flask app / routes
  config.py
  models.py                     # Pydantic API DTOs
  wikidata/
    provider.py                 # protocol / contracts
    entity_data.py              # optional native TTL implementation
    sparql.py                   # optional WDQS implementation
    entity_search.py
    cache.py
  graph/
    store.py                    # RDFLib + state/projected NetworkX
    mapping.py                  # property policy and semantics
    paths.py                    # BFS / weighted path algorithms
    turtle_projection.py        # canonical compact TTL + IDs/spans
  agent/
    client.py                   # reuse OpenRouter setup
    tools.py
    investigator.py
    prompts.py
  investigations/
    service.py
    state.py
    events.py
    replay.py
  ui/
    index.html
    css/
    js/
    vendor/
data/
  wikidata/                    # cached data (document git strategy)
  fixtures/
    cases/
    event_traces/
docs/
  wikidata-ingestion-decision.md
  workshop-runbook.md
tests/
  test_pathfinding.py
  test_strength.py
  test_graph_mapping.py
  test_turtle_projection.py
  test_provider.py
  test_investigation.py
  test_api.py
```

This is **a suggested logical layout**, not a demand for a file per tiny helper. Consolidate modules when doing so makes the implementation clearer. Preserve existing modules until replacements pass tests.

### Dependencies

Current: Flask, RDFLib, `openai`, dotenv, `uv`. Likely add: `networkx`, `pydantic`, `httpx` (or `requests`), `pytest`. Prefer standard library where adequate. No need for Neo4j, LangChain/LangGraph, Celery, Postgres, React, or a vector DB for MVP.

---

## 10. Execution milestones and acceptance criteria

### Milestone 0 — Repository stabilization and baseline

**Tasks:** set up/test `uv sync`; document current app entry points; preserve the existing editor behavior; add unit-test skeleton, `.env.example`, dev commands. Identify licenses for copied frontend libraries.

**Done when:** the original editor still opens TTL, renders triples and supports two-way highlighting; Python tests run in CI/local; new feature starts from an unbroken baseline.

### Milestone 1 — Wikidata ingestion spike (**decision gate**)

**Tasks:** implement a thin provider POC for at least two approaches, test QID lookup and bounded outgoing/incoming expansion, compare latency/coverage, validate 3 candidate person pairs, document a chosen architecture and fallback.

**Done when:** `docs/wikidata-ingestion-decision.md` records evidence, decision, limitations, and a reproducible command that materializes one small real graph to disk.

**Do not proceed to a final live-data architecture until this gate is satisfied.** Continue other local work in parallel with fixtures if needed.

### Milestone 2 — Graph model, policy and pathfinding

**Tasks:** implement property allowlist/config, RDFLib ingestion/projection, NetworkX multigraph model, shortest and strongest path results, deterministic tie-breaking, metadata and tests.

**Done when:** a small synthetic graph proves shortest ≠ strongest where appropriate, shortest/strongest can coincide, parallel edges preserve their predicates, and truncation/partial optimality is accurately flagged.

### Milestone 3 — Deterministic non-LLM investigation

**Tasks:** implement bounded bidirectional expansion using provider/cache and pure path tools; emit investigation events; build replay fixtures from actual cached results.

**Done when:** the system can find and display paths for **at least two** pretested real-person pairs without an LLM or live network access (snapshot/replay), and can return an honest no-path/partial result.

### Milestone 4 — Agentic exploration

**Tasks:** refactor existing OpenRouter client, add typed tools and bounded tool loop, event log, cancellation, fallback mode. Ensure decisions are based on search status and tools, not invented graph facts.

**Done when:** at least one test scenario has the agent select an additional exploration action based on observed results, produces the same verifiable path result as the deterministic backend, and terminates within budget. No API key produces a clear fallback state.

### Milestone 5 — Workbench UI

**Tasks:** migrate existing editor as main UI, progressive vis-network updates, timeline, endpoint search, path comparison, bidirectional highlighting, source inspector, live/replay badges.

**Done when:** a full recorded investigation can be played through the UI, clicking a tool event highlights its edges, clicking an edge selects matching Turtle, selecting Turtle highlights the graph, and final paths are toggleable without rerendering the entire network.

### Milestone 6 — Workshop reliability and polish

**Tasks:** create 3 authentic pretested demo pairs, capture replay traces, implement offline assets, make a runbook with exact click-by-click flow, test rate limiting/network failure, improve layout/labels, add visual regression or E2E smoke tests if feasible.

**Done when:** cold-start offline replay works; one live lookup works in a healthy network environment; presenter can complete demo with no editing code or terminal interaction; no unverified or fabricated path is labeled as fact.

---

## 11. Automated test plan

**Algorithms:**
- shortest chooses fewer eligible edges; stable tie-breaking;
- strongest chooses a longer path when its policy-defined cost is lower;
- cycles, disconnected components, duplicate/parallel relations and directed/inverse edges;
- weak institutional hubs don't overshadow clearly stronger links under configured policy;
- hop-bound and budget enforcement; nonnegative costs;
- a partial graph never receives a false global-optimality claim.

**Wikidata provider:**
- fixture-based entity search and ambiguity handling;
- outbound + inbound handling; `429`/timeout/invalid IDs/retries;
- response truncation, `complete` semantics, caching, duplicate statement IDs;
- normalization preserves QID/PID/source and source direction.

**RDF/UI:**
- generated Turtle re-parses with RDFLib;
- backend edge/triple ID mapping survives order changes;
- unsupported Turtle syntax rejected/handled gracefully;
- source selection and graph selection highlight the same statement;
- applying event deltas doesn't delete unrelated nodes or reset layout.

**Agent/integration:**
- model tool calls rejected when outside allowlist or budget;
- mocked agent chooses a second expansion after insufficient first results;
- the final answer can cite only returned `PathResult` edges;
- replay produces exactly the same final graph/paths as the recorded event log;
- missing `OPENROUTER_API_KEY` and offline endpoint states are handled explicitly.

**Demo acceptance:**
- two real Wikidata QIDs → progressively expanded evidence graph → two computed path result modes;
- every displayed relationship opens an appropriate Wikidata entity/property reference;
- graph selection ↔ Turtle source correspondence works;
- no invented connection is produced if search fails;
- reproducible offline demo and clear source-data snapshot date.

---

## 12. Performance and safety budgets (configurable starting defaults)

These are **proposed safeguards**, not measured optimum settings:

```yaml
search:
  max_hops: 6
  max_explored_nodes: 400
  max_explored_edges: 1500
  max_edges_per_expansion: 100
  max_provider_requests: 25
  max_agent_tool_calls: 18
  wall_time_seconds: 90
  allow_live_fallback_to_cache: true
ranking:
  hop_penalty: 0.20
  minimum_strength: 0.05
ui:
  max_visible_nodes: 250
```

Adjust after the ingestion POC and actual demo benchmarks. If a cap is hit, return **partial** with reason and explored scope. The public demo should favor readable graphs over raw node count.

---

## 13. Decisions intentionally left open

The coding agent should **not silently lock these in without evidence**:

1. **RDF acquisition:** direct `.ttl` entity downloads vs WDQS projection vs JSON→RDF; compare using Milestone 1.
2. **Incoming links:** how to discover reverse/inbound relationships without unacceptable endpoint cost.
3. **Relationship allowlist and strengths:** initial weights require validation against actual Wikidata links and workshop pair coverage.
4. **Demo pairs:** find and verify several real pairs; do not invent a plausible historical chain.
5. **Graph edit mode:** read-only Wikidata source plus optional sandbox overlay is preferred; confirm if interactive editing belongs in MVP or polish milestone.
6. **Event streaming transport:** SSE likely sufficient; replay should use the same event contract regardless.
7. **Optimality statement:** design a future certification rule only if needed; explored-graph ranking suffices for the workshop.

For each open decision, document the chosen option, evidence, tradeoffs, and fallback.

---

## 14. Coding-agent execution instructions

1. **Inspect actual files before edits**; use the supplied repo as source of truth rather than assuming this plan exactly matches a later branch.
2. Keep changes incremental. Do not replace the working live editor with a new framework unless a measured limitation justifies it.
3. Implement and test deterministic pathfinding and fixtures **before** introducing the LLM tool loop.
4. Prioritize an **offline, repeatable, visually understandable demo** over supporting arbitrary person pairs at unlimited scale.
5. Keep domain facts, ontology semantics, ranking policy and dynamic investigation state separate.
6. Use Wikidata IDs/URIs consistently; never infer interpersonal contact merely from a shared affiliation.
7. Expose uncertainty/coverage honestly in API and UI. Do not market explored paths as global optima.
8. Update README and `docs/workshop-runbook.md` with exact setup, live and offline demo commands, environment variables, provider behavior, and limitations.
9. After each milestone, run tests and verify editor↔graph↔agent highlighting manually or with E2E tests. Fix regressions before advancing.
10. When a major external-data design issue arises, execute Milestone 1 comparison and record it; avoid refactoring the entire app around an untested Wikidata API assumption.

## 15. Definition of done (one sentence)

**A presenter can choose two real people, watch a bounded agent investigation progressively reveal sourced Wikidata relationships, compare the shortest and strongest paths in the explored graph, and click any path or tool event to inspect its exact edges and Turtle statements—reliably, including in offline replay mode.**
