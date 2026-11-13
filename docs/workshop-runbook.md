# Workshop runbook: Six Degrees of Knowledge

How to prepare, run and rescue the live demo. Every step is a click or one terminal command. Once the server is running, nothing requires editing code or using the terminal.

## What the demo shows (30-second version)

Pick two notable people. An AI agent (or a fixed deterministic strategy) explores Wikidata relationships outward from both of them, inside a local snapshot of Wikidata. The graph grows live.

When the two sides meet, the backend computes two answers. **Shortest** uses the fewest relationships. **Strongest** has the lowest weighted cost: family and teacher/student links count for more than a shared employer or school. Every edge is a Wikidata statement you can click; it highlights the exact Turtle triple and links to Wikidata.

## Honesty rules for the presenter

Say these out loud, because the UI says them too:

- Results are the shortest or strongest **among the explored relationships**, not the global optimum over all of Wikidata.
- A shared employer, school or organisation is an **association**, not proof that two people met.
- A statement is what **Wikidata records**, not a verified fact. A missing edge means "unknown", not "false".
- **REPLAY** means recorded events played back. Never present a replay as a live run (the badge says which).
- Sandbox edges are **hypothetical** (pink, dashed, "With sandbox" tab). They never change Wikidata facts.

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
   - Optionally set `LLM_PROVIDER=deepseek|openrouter` and `DEEPSEEK_MODEL` / `OPENROUTER_MODEL`.
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
   - AI-agent mode needs network access to the LLM provider.
3. Projector: aim for 1366×768 or larger. If the screen is narrow, hide the source pane with **Source** in the graph bar.
4. Disable sleep and notifications; close other heavy apps (the people graph is a large SQLite file).

## Start (5 minutes before)

```bash
uv run python -m src.server        # http://localhost:5000
```

Open **http://localhost:5000** in Chrome. Check the status bar at the bottom right:
- **Server online**;
- the data source and snapshot date.

The mode menu shows **AI agent (model)** when a key is configured.

Tip: a deep link pre-fills a pair, for example `http://localhost:5000/?source=Q…&target=Q…&mode=agent`.

## Demo flow (8–12 minutes)

| # | Action (click) | What to say / point at |
|---|---|---|
| 1 | Show the empty workbench. | Toolbar: two people, strategy, relationship groups. Bottom right: the data source and **snapshot date** (offline Wikidata snapshot). |
| 2 | Type the first person's name and pick them from the list. Do the same for the second. | Names come from Wikidata labels (English and Spanish); each person has a stable QID. |
| 3 | Leave **Family**, **Teacher / student**, **Shared institution** and **Shared film / music** on. Mode: **AI agent**. Click **Explore**. | Badges: **LIVE** and **AI agent · model**. The two people are pinned left and right. |
| 4 | Watch the timeline. | Each indented row is a tool call the agent chose, under a one-line decision. Rows show how many edges came back and whether the result was truncated. The agent picks *what* to explore; the backend fetches, computes and verifies. |
| 5 | A path appears (orange "Path found" row). | Result panel. **Shortest** = fewest relationships. **Strongest** = lowest cost. If they differ, open both tabs and show why: family/mentorship beats a shared school. The agent's summary is shown only if it mentions nothing but path entities. |
| 6 | Click an edge on the path. | The Turtle source highlights that exact triple: `wd:Q… wdt:P… wd:Q… .` The inspector shows subject, property and object with Wikidata links. "This is one RDF statement." |
| 7 | Click a timeline step. | Those are the edges that step fetched. |
| 8 | Select a few lines in the Turtle source. | The matching nodes and edges light up. The text and the graph are two views of the same triples. |
| 9 | Optional: select the two people, click **+ sandbox edge**, change the PID if you like, then **Apply sandbox**. | A **With sandbox** tab appears with a hypothetical result. Pink dashed edge, "NOT Wikidata" banner. Wikidata facts are untouched; **Clear** removes it. |
| 10 | Optional: open **Weak links**, turn on e.g. **Birthplace (weak)** or **Awards (weak)**, and click **Explore** again. | Shows how the relationship policy changes the answer ("both born in Medellín" is a fun but weak link). |
| 11 | Close. | Enterprise analogy: beneficial-ownership investigations, cross-domain entity discovery. The same pattern (typed relationships, a bounded agentic search, verified paths, provenance per edge) applies. |

**Replay instead of live:** pick a case in **Replay a demo case…**. The badge reads **REPLAY · recorded <time>**. Say it is a recording.

## If something goes wrong

| Symptom | What to do |
|---|---|
| AI agent is slow, errors, or the mode is greyed out ("no API key") | Switch the mode to **Deterministic (no AI)** and click **Explore**, or use **Replay**. Both work offline. |
| No internet at the venue | Replay and deterministic exploration are fully local. Only AI-agent mode needs the LLM provider. |
| Status bar says **Server offline** | In the terminal: `uv run python -m src.server`, then reload the page. The previous result stays on screen until a new run starts. |
| "Could not start: …" | Read the message. Usually the people weren't picked from the suggestions (pick them from the list), or no relationship group is on. |
| The result says **Partial result** | That is honest behaviour: a budget (time, requests, nodes) ran out. Say so; show the explored graph and any path found so far, or use the prepared replay. |
| "No connection found" | No path among the explored relationships within the budget. It is not a claim that none exists. Use a prepared pair. |
| The graph is too busy | Click **Fit**. Click a path tab to dim the rest. **Show all** reveals hidden leaf nodes; by default they are capped. |
| Wrong person picked | Click **Reset**. |
| The run takes too long | Click **Cancel**. The partial result stays. |
| People graph missing on this laptop | Only replays are available; the status bar says so. Use the prepared cases. |

## Reference

- Ports and URLs: `http://localhost:5000/` (workbench), `/legacy` (old retail demo), `/api/health`.
- Environment variables:
  - `PEOPLE_GRAPH_PATH` (default `data/wikidata/people_graph.sqlite`);
  - `DEMO_CASES_PATH` (default `config/demo_cases.yaml`);
  - `LLM_PROVIDER`, `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`.
- Budgets (`config/search.yaml`): 25 neighbour requests, 400 nodes, 1500 edges, 18 agent tool calls, 90 s. The UI shows at most 250 nodes at once.
- Relationship policy (`config/relationships.yaml`): groups, strengths and the hub cap (organisations above it can't be intermediate path nodes).
- Data: the Wikidata truthy dump of the snapshot date. Only statements whose subject is a human; English and Spanish labels.
