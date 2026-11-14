# Workshop runbook: Six Degrees of Knowledge

How to prepare, run and rescue the live demo. Every step is a click or one terminal command. Once the server is running, nothing requires editing code or using the terminal.

## What the demo shows (30-second version)

Pick two notable people. An LLM agent (or a deterministic baseline with no LLM) expands the knowledge graph outward from both of them, one entity at a time, inside a local snapshot of Wikidata. The graph grows live.

When the two sides meet, the path-search tool finds the **strongest** path: the one with the lowest weighted cost, where family and teacher/student links count for more than a shared employer or school. (It also computes the shortest path internally, but the workbench shows only the strongest one, so the audience follows a single answer.) Every edge is a Wikidata statement you can click; it highlights the exact Turtle triple and links to Wikidata.

## Honesty rules for the presenter

Say these out loud, because the UI says them too:

- Results are the strongest **within the explored subgraph**, not the global optimum over all of Wikidata.
- A shared employer, school or organisation is an **association**, not proof that two people met.
- A statement is what **Wikidata records**, not a verified fact. A missing edge means "unknown", not "false".
- **REPLAY** means recorded events played back. Never present a replay as a live run (the badge says which).
- Sandbox edges are **hypothetical** (pink, dashed, "With sandbox" tab). They never change Wikidata facts.

## Vocabulary (say it this way)

The demo teaches two fields, so use their terms, on screen and out loud.

| Say | Meaning here | Avoid |
|---|---|---|
| **entity** (QID), e.g. Q37181 | a node: a person, work, organisation or place | "item", "record" |
| **property** (PID), e.g. P161 *cast member* | the type of an edge | "field", "link type" |
| **statement** / **triple** | one edge: entity, property, entity (one Turtle line) | "relationship row", "link" |
| **hop** / **path** | one statement on a route / a route from one person to the other | "degree of separation" (only loosely) |
| **explored subgraph** | everything fetched so far; results are limited to it | "the database", "everything" |
| **frontier** | explored but not yet expanded entities; where the search can grow | "candidates list" |
| **hub** | a very well-connected organisation or place; never used as a stepping stone | |
| **LLM agent** | the model that chooses each tool call | "the AI" (vague) |
| **agent turn** | one model message plus the tool calls it issued together | "the agent thinks" (it is not chain of thought) |
| **tool call** → **tool** → **tool result** | the model's request → the deterministic code that runs it → what comes back | "the backend" |
| **deterministic baseline** | the same tools driven by a fixed ranking rule, no LLM | "no AI mode" |
| **grounding check** | the final answer may name only entities on the verified path | "fact check" |
| **budget** | caps on tool calls, expansions, entities and time that end a run as *partial* | |

## One-time setup (days before)

1. `uv sync`
2. Build the offline people graph (see `docs/wikidata-ingestion-decision.md`):
   ```bash
   bash scripts/download_wikidata_dump.sh          # ~72 GB, resumable, MD5-verified
   bash scripts/extract_people_slice.sh data/wikidata/raw/wikidata-<date>-truthy-BETA.nt.gz   # ~13 min, JOBS=nproc
   uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
       --out data/wikidata/people_graph.sqlite \
       --dump-meta data/wikidata/raw/wikidata-<date>-truthy-BETA.nt.gz.meta.json
   ```
3. Configure an LLM in `.env` (only needed for live AI-agent runs):
   - `DEEPSEEK_API_KEY=...` (DeepSeek, OpenAI-compatible), **or**
   - `OPENROUTER_API_KEY=sk-or-...` (OpenRouter).
   - Optionally set `LLM_PROVIDER=deepseek|openrouter` and `DEEPSEEK_MODEL` / `OPENROUTER_MODEL` (DeepSeek default: `deepseek-flash`).
   - `DEEPSEEK_THINKING=disabled` (default) keeps agent turns at 1–3 s. `enabled` makes the model reason first: smarter on hard pairs, but 15–70 s per turn, so a live run needs a higher `wall_time_seconds` than 90.
4. Choose the hub cap (organisations above it can't be intermediate path nodes):
   ```bash
   uv run python scripts/analyze_hubs.py --out hubs.json            # degree distribution + cap sweep
   uv run python scripts/analyze_hubs.py --seeds config/demo_seeds.txt --cross recent historical
   ```
   Pick the cap that keeps most pairs connected while fewer shortest paths lean on giant hubs. Set `pathfinding.max_intermediate_degree` in `config/relationships.yaml` and bump its `version`. Do this **before** recording cases: preflight fails replays recorded under another policy.
   - Node kinds can have their own cap in `pathfinding.max_intermediate_degree_by_kind`. Places use 1,000 (policy v4): birthplace is on by default, and only small towns should connect people. `analyze_hubs.py --caps` sweeps the global cap only; the per-kind caps stay as configured.
5. Choose and record the demo pairs:
   ```bash
   uv run python scripts/find_demo_pairs.py --seeds config/demo_seeds.txt --resolve-only    # check names → QIDs
   uv run python scripts/find_demo_pairs.py --seeds config/demo_seeds.txt --cross recent historical        --pairs 400 --out candidates.json                                                    # suggestions only
   uv run python scripts/find_demo_pairs.py --pairs 60                                     # or: random well-connected people
   uv run python scripts/investigate.py --search "Marie Curie"                   # look up QIDs
   uv run python scripts/investigate.py <SRC> <TGT> --record <id>                # deterministic replay
   uv run python scripts/investigate.py <SRC> <TGT> --record <id>-agent --agent  # AI-agent replay
   ```
   - `config/demo_seeds.txt` has two groups: `[historical]` (~58: Latin America, Brazil, physics, computing, music, painting, philosophy, politics) and `[recent]` (~77 people everyone knows today: Brazilian football, music, TV and influencers, Medellín reggaetón, global pop culture). `--cross recent historical` pairs only across the groups (influencer ↔ historical figure is the fun demo). Without `--cross` every pair is tried (~9,000); `--pairs N` takes a random sample.
   - Lines marked `!` in `--resolve-only` are ambiguous names or low-degree matches (possibly a namesake): replace the name with the right QID.
   - Each `--record` checks its output before writing. A deterministic run must be reproduced exactly from its fixture; an agent trace must rebuild the same graph and paths. Runs that find no path are not recorded.
   - Paste the printed entry into `config/demo_cases.yaml`.
   - Aim for **at least 3** pairs. At least one should have **Strongest ≠ Shortest** (a longer family or mentorship chain that beats a shared institution), if the data supports it.
   - Commit `config/demo_cases.yaml` and `data/fixtures/`.

## Day before

1. `uv run python -m src.preflight --online` must print `READY` (0 failures). It checks:
   - the configuration;
   - every demo case's replay (it rebuilds the graph and recomputes the paths);
   - the people graph;
   - that UI assets are local, with no CDN;
   - the LLM key, with one tiny call;
   - that the port is free.
2. Rehearse once with the network **disconnected**:
   - replays and deterministic exploration must work;
   - AI-agent mode needs network access to the LLM provider. Without it, an agent run stops within seconds and the result panel says the language model could not be reached; switch the mode to **Baseline** or use a demo replay.
   - Rehearsed 2026-11-13 with outbound network blocked: all replays, a live deterministic Neymar–Snoop Dogg run (path in 7 s, ~1,450 nodes explored, 250 drawn) and the agent-unreachable message all worked.
3. Projector: aim for 1366×768 or larger. The Source and History panes start minimized; keep them that way on a narrow screen.
4. Disable sleep and notifications; close other heavy apps (the people graph is a large SQLite file).

## Start (5 minutes before)

```bash
uv run python -m src.server        # http://localhost:5000
```

Open **http://localhost:5000** in Chrome. Check the status bar at the bottom right:
- **Server online**;
- the data source and snapshot date.

The **LLM agent** mode button is enabled when a key is configured (its tooltip names the model).

Tip: a deep link pre-fills a pair, for example `http://localhost:5000/?source=Q…&target=Q…&mode=agent`.

## Demo flow (8–12 minutes)

| # | Action (click) | What to say / point at |
|---|---|---|
| 1 | Show the empty workbench. | Toolbar: two people, strategy, relationship groups. Bottom right: the data source and **snapshot date** (offline Wikidata snapshot). |
| 2 | Type the first person's name and pick them from the list. Do the same for the second. | Names come from Wikidata labels (English and Spanish); each person has a stable QID. |
| 3 | Leave **Family**, **Teacher / student**, **Shared institution** and **Shared film / music** on. Mode: **LLM agent**. Click **Explore**. | Badges: **LIVE** and the model name. The two people are pinned left and right. |
| 4 | Watch the timeline. | Each indented row is a tool call the agent made, under that turn's message. Rows show how many statements came back and whether the result was truncated. The LLM picks *what* to explore; the tools are deterministic code that query the snapshot, run the path search and verify the result. |
| 5 | A path appears (orange "Path found" row). | History panel (open it with **+** on the right strip). The **strongest** path is the one with the lowest cost: point out its family/mentorship links, which beat a shared school. The agent's summary is shown only if it mentions nothing but path entities. |
| 6 | Click an edge on the path. | The Turtle source highlights that exact triple: `wd:Q… wdt:P… wd:Q… .` The inspector shows subject, property and object with Wikidata links. "This is one RDF statement." |
| 7 | Click a timeline step. | Those are the edges that step fetched. |
| 8 | Select a few lines in the Turtle source. | The matching nodes and edges light up. The text and the graph are two views of the same triples. |
| 9 | Optional: select the two people, click **+ sandbox edge**, change the PID if you like, then **Apply sandbox**. | A **With sandbox** tab appears with a hypothetical result. Pink dashed edge, "NOT Wikidata" banner. Wikidata facts are untouched; **Clear** removes it. |
| 10 | Optional: open **Relations ▾**, tick e.g. **Awards** or **Political party** under "Weak links", and click **Explore** again. | Shows how the relationship policy changes the answer ("both born in Medellín" is a fun but weak link). |
| 11 | Close. | Enterprise analogy: beneficial-ownership investigations, cross-domain entity discovery. The same pattern (typed relationships, a bounded agentic search, verified paths, provenance per edge) applies. |

**Step-through (presenter-paced, the only mode):** every run, live or replay, is revealed one step at a time: one agent turn (the model's message and its tool calls), then each tool call with its tool result (or, for the baseline, one expansion with its ranking rule). A card at the top left of the graph shows the step: the model's own words first, then the tool call and its result; the camera frames the graph to the right of the card. The last step shows the connection as a chain of people, works and organizations; the graph, timeline, Turtle and paths show only what was known at that step, and the step's new edges are highlighted.
- **Next ▶** / **◀ Back**, or →, Space, Page Down / ←, Page Up (a presenter remote works). **−** minimizes the card to its title row (Back / Next stay available). The meters at the bottom of the card turn amber near a budget limit and red when one is used up.
- **Path moments:** when the path first appears, the card says where the two sides met ("The two sides met at Snowden!") and which statement joined them. When a stronger path replaces it, the card says why ("1 hop longer, but 3 strong links instead of 2"), the old path stays dashed in the graph for that step, and links are labelled strong / medium / weak (family and teacher/student are strong; a shared film is medium; a shared school, employer or birthplace is weak).
- **Who chose what:** next to each planned expansion, the card says whether the tools ranked it ("ranked #1 by get_search_status", "lead #2 from the tools") or it was "the agent's own pick". Older recordings didn't store those lists; their cards say so instead. Point this out: the LLM decides, but the tools do the ranking.
- After the connection appears, the rest of the search (the agent following leads that never beat the path) is **one step**: "Nothing stronger turned up", listing who it expanded. Say: "it kept looking for something stronger, like family links, and found nothing better", then Next to the result. If someone asks what it tried, **Step through these N steps one by one** unfolds them. For the "★ A stronger path replaced the old one" moment, use `coelho-senna-agent` (the only case that has it).
- Live runs keep running in the background; the card says how many steps are ready ("3 more ready") or that the agent is still working.
- The card quotes only what the agent actually wrote, plus facts from the tools (arguments, results, budgets). "(no message…)" means the model went straight to its tool calls.
- Nothing plays itself out: to reach the result, keep pressing **Next**.
- An agent turn takes several steps: first what the agent decided and the calls it planned, then one step per call ("Turn 3 · call 2 of 3"), each with what the tool does and what it returned. ★ marks the call that connected the two sides.
- Tools that don't change the graph get an effect: `compute_paths` sends a pastel ping out from both people along every edge the path search can walk, hop by hop to the far end, colouring each edge as it crosses it; the colour then fades and, after a short pause, the next ping leaves (the search runs from both ends; hubs are skipped, as in the real search); `get_search_status` sweeps a radar beam around each person that lights exactly the frontier entities the tool ranks, then shows the top suggestions the agent was given as green #1…#8 pills (listed on the card). On the agent's next decision the card says whether each chosen entity was ranked by `get_search_status` or was the agent's own pick, which is a good moment to point out that the LLM decides and the tool only ranks. Recordings made before 2026-11-14 carry no suggestions, so they show only the radar. Both loop until you press Next. They show what the tool does, not its answer.
- The graph tells the story: expanded people carry their order (① ② ③…), the current step's additions are green, and earlier dead ends fade to grey. The camera frames the trail below the card on each step.
- The Source pane and the History pane (result + timeline) start **minimized** so the step card and graph carry the story; **+** on their strips opens them, **−** minimizes them again. The step card has its own **−**. The detail box for a clicked node or edge opens in the graph's bottom-right corner and has its own **−**.

**Replay instead of live:** pick a case in **Demo replays…** (grouped LLM agent / Deterministic baseline). The badge reads **REPLAY · <recording time>**, and the mode switch shows how that run was explored. Say it is a recording.

**Save a live run as a demo replay:** when a live run ends with a connection, its last step card has **Save as demo replay…**. Pick a case id (lowercase, digits, hyphens; a free one is suggested) and a title, then **Save**. The server re-checks the run (a baseline run must replay identically from its fixture; an agent run's trace must rebuild the same paths), writes `data/fixtures/cases/<id>.json` and `data/fixtures/event_traces/<id>.json`, and appends the case to `config/demo_cases.yaml`. It appears in **Demo replays…** at once. Existing cases are never overwritten, and replays and runs without a path cannot be saved. Commit the three files to keep the case; run `uv run python -m src.preflight` afterwards.

### Prepared cases (`config/demo_cases.yaml`, snapshot 2026-11-04)

| Case | Mode | Story |
|---|---|---|
| `lula-snoop-agent` | LLM agent | **Headliner.** Lula → *South of the Border* → Oliver Stone → *Dave* → Ving Rhames → *Baby Boy* → Snoop Dogg (6 hops, all film credits; the two sides meet at step 20). Shows the ranking (the agent follows #1 and #7). Recorded with DeepSeek reasoning on; live without reasoning it is usually **not** found, so replay it. |
| `beyonce-alice-braga-agent` | LLM agent | **Ranking at work:** the agent expands the tools' #1, #2 and #3. Strongest path at the first connection (step 9): Beyoncé → Solange → *Ghost Whisperer* → Sônia Braga → Ana Braga → Alice (5 hops), stronger than the 4-hop route that ends in a weaker "relative" link. Then the search for something stronger folds into one step. |
| `diana-churchill-agent` | LLM agent | **Short and clear (10 steps).** The agent follows the ranking (#1 Charles III, #5 Lord Randolph Churchill) and the sides meet at step 8: Diana → Charles III → Cheam School ← Lord Randolph Churchill → Winston (4 hops, family at both ends), stronger than the 3-hop route through her father and Sandhurst. |
| `coelho-senna-agent` | LLM agent | **The only case where the path improves mid-run** ("★ A stronger path replaced the old one", step 28): Paulo → United Nations → Pelé → Xuxa → Senna (4 hops, met at step 14) is replaced by Paulo → Renata Sorrah → *Um Anjo Caiu do Céu* → Luciano Szafir → Xuxa → Senna (5 hops, 3 strong links instead of 2). Also the best case for **who chose what**: after the first path every pick is tagged "lead #k from the tools". |
| `neymar-snoop-agent` | LLM agent | Neymar → *xXx: Return of Xander Cage* → Ice Cube → *I Got the Hook Up* → Snoop Dogg. Also found **live** in deterministic mode (~7 s). |
| `neymar-icecube-agent`, `kanye-gisele-agent` | LLM agent | 2-hop warm-ups (*xXx*; the documentary *Mademoiselle C.*). |
| `diana-churchill`, `coelho-senna`, `charles-bohr` | Deterministic | No-AI fallbacks; shortest = strongest. |

Before the workshop, check on Wikidata: Charles III's and Bohr's "Trinity College" are the same item, and Coelho's "employer: United Nations".

## If something goes wrong

| Symptom | What to do |
|---|---|
| The LLM agent is slow, errors, the mode is greyed out ("no API key"), or the result says the language model **could not be reached** | Switch the mode to **Baseline** and click **Explore**, or use **Demo replays…**. Both work offline. |
| No internet at the venue | Replay and deterministic exploration are fully local. Only AI-agent mode needs the LLM provider. |
| Status bar says **Server offline** | In the terminal: `uv run python -m src.server`, then reload the page. The previous result stays on screen until a new run starts. |
| "Could not start: …" | Read the message. Usually the people weren't picked from the suggestions (pick them from the list), or no relationship group is on. |
| The result says **Partial result** | That is honest behaviour: a budget (time, requests, nodes) ran out. Say so; show the explored graph and any path found so far, or use the prepared replay. |
| "No connection found" | No path in the explored subgraph within the budget. It is not a claim that none exists. Use a prepared pair. |
| The graph is too busy, or the path is off-screen | Click **Fit** (whole graph). Click **Show** in the path panel to dim everything but the strongest path. **Show all** reveals hidden leaf nodes; by default only 250 nodes are drawn. |
| Wrong person picked | Click **Reset**. |
| The run takes too long | Click **Stop** (the Explore button during a run). The partial result stays. |
| People graph missing on this laptop | Only replays are available; the status bar says so. Use the prepared cases. |

## Reference

- Ports and URLs: `http://localhost:5000/` (workbench), `/legacy` (old retail demo), `/api/health`.
- Environment variables:
  - `PEOPLE_GRAPH_PATH` (default `data/wikidata/people_graph.sqlite`);
  - `DEMO_CASES_PATH` (default `config/demo_cases.yaml`);
  - `LLM_PROVIDER`, `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_THINKING`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`.
- Budgets (`config/search.yaml` v3): 40 neighbour requests, 2,000 nodes, 7,000 edges, 30 agent tool calls, 90 s. The frontier prefers well-connected nodes (`docs/search-tuning.md`). The UI shows at most 250 nodes at once.
- Relationship policy (`config/relationships.yaml`): groups, strengths and the hub cap (organisations above it can't be intermediate path nodes).
- Data: the Wikidata truthy dump of the snapshot date. Statements whose subject is a human, plus film/music credits and founders pointing at a human; English and Spanish labels (falling back to Wikidata's language-independent label).
