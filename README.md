# knowledge-graph-workshop

A playground for learning how knowledge graphs and ontologies work. It includes a graph visualizer, a live Turtle editor and an LLM-powered assistant.

## Status

This repository is being extended into **Six Degrees of Knowledge**, an agentic explorer that connects two notable people through Wikidata relationships. See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the specification and [`CLAUDE.md`](CLAUDE.md) for contributor guidance.

- **Milestone 0 (baseline) is complete:** dependencies, test suite, an offline live editor and `.env.example`.
- The retail/supermarket assets below are **legacy examples**. They still work, but new features are not built on them.

## How to run the demo

### 1. On a fresh clone: recorded demos only, no data needed

The recorded demo runs (replays) need only files that are in the repo: `data/fixtures/` and `config/demo_cases.yaml`. You don't need the people graph or an API key.

```bash
git clone https://github.com/martelosso/kg-find-connection.git
cd kg-find-connection
uv sync
uv run python -m src.server
```

Open http://localhost:5000, choose a case in **Demo replays…**, and step through it with **Next** / **Back** (or the → and ← keys). The badge reads **REPLAY**, with the time of the recording.

**macOS:** the AirPlay Receiver already listens on port 5000, and the browser then shows "Access to 127.0.0.1 was denied" (HTTP 403). Start the server on another port with `PORT=5050 uv run python -m src.server` (or put `PORT=5050` in `.env`) and open http://localhost:5050. Turning off System Settings → General → AirDrop & Handoff → AirPlay Receiver frees 5000 too.

Without the people graph you can only play the recorded cases. The status bar says so, and `uv run python -m src.preflight` reports **NOT READY** because the people graph is missing.

### 2. Live searches between any two people: add the people graph

The people graph (`data/wikidata/`) is gitignored because it is too big for git. To get it, do one of the following:

- **Copy it** from a machine that has it: `data/wikidata/people_graph.sqlite` (about 2.5 GB). The server needs only this file. The `people_graph.sqlite.adjacency/` folder next to it is a speed index used by the prep scripts (`find_demo_pairs.py`, `benchmark_investigations.py`). They rebuild it themselves if it is missing.
- **Build it** from the Wikidata dump: about 72 GB to download, then about 19 minutes of processing. Run these in Git Bash:

  ```bash
  scripts/download_wikidata_dump.sh                       # → data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz
  scripts/extract_people_slice.sh data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz
  uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
      --out data/wikidata/people_graph.sqlite \
      --dump-meta data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz.meta.json
  ```

  See [`docs/wikidata-ingestion-decision.md`](docs/wikidata-ingestion-decision.md) for details. To use a build stored elsewhere, set `PEOPLE_GRAPH_PATH` to its path.

With the people graph present, pick any two people and click **Explore** in **Baseline** mode, which uses no LLM.

### 3. LLM agent mode: add an API key

```bash
cp .env.example .env    # then set DEEPSEEK_API_KEY or OPENROUTER_API_KEY
```

The agent explores the people graph live, so it needs both the key and step 2. Without a key, the **LLM agent** option is disabled and you can still use Baseline mode and the replays.

### Before a workshop

Run `uv run python -m src.preflight --online` until it says **READY**, then follow [`docs/workshop-runbook.md`](docs/workshop-runbook.md).

## Project contents

| Path | What it is |
|---|---|
| [`graph-view/live-editor.html`](graph-view/live-editor.html) | Standalone live Turtle editor (CodeMirror 5) with a vis-network graph and two-way highlighting. It works offline, using the libraries vendored in `src/ui/vendor/`. The workbench is built on its parser, theme and highlighting. |
| [`graph-view/visualize.html`](graph-view/visualize.html) | Older standalone Turtle graph visualizer (legacy). |
| [`ontology/people_connections.ttl`](ontology/people_connections.ttl) | The people graph's teaching ontology: classes, relation groups and the 26 Wikidata properties with domain and range. Shown in the workbench's **Ontology** view; a test keeps it in step with `config/relationships.yaml`. |
| [`ontology/ontology.owl.ttl`](ontology/ontology.owl.ttl), [`data/mock_data.ttl`](data/mock_data.ttl) | Hand-written supermarket/retail ontology and instance data (legacy). |
| [`src/ontology_agent.py`](src/ontology_agent.py) | Retail assistant that queries the graph with SPARQL through an LLM on OpenRouter, from the terminal (legacy). |
| [`src/server.py`](src/server.py) + [`src/ui/`](src/ui/) | Flask app: the Six Degrees workbench at `/` (`src/ui/index.html`, `js/`, `css/`, `vendor/`), the investigation API under `/api/`, and the legacy retail assistant at `/legacy`. |

## Setup

Requires Python 3.13 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # then fill in the values
```

`.env` is gitignored. It holds:

- `DEEPSEEK_API_KEY` or `OPENROUTER_API_KEY`: needed for the LLM agent (and the legacy retail assistant, which uses OpenRouter). `LLM_PROVIDER` picks one when both are set.
- `DEEPSEEK_MODEL` / `OPENROUTER_MODEL`: optional model overrides.
- `WIKIDATA_USER_AGENT`: needed later for live Wikidata requests. Use the format `name/version (contact)`.

Do not commit or share your API key.

## Entry points

```bash
# Retail assistant, one question from the terminal (needs OPENROUTER_API_KEY)
uv run python -m src.ontology_agent "What classes exist for pricing?"

# Retail assistant, interactive terminal session
uv run python -m src.ontology_agent

# Flask web UI, then open http://localhost:5000
uv run python -m src.server
```

**Live editor:** open `graph-view/live-editor.html` directly in a browser. Use **Open .ttl** to load a file such as `ontology/ontology.owl.ttl`. No network is required.

**Legacy visualizer:** open `graph-view/visualize.html` and load a `.ttl` file with its file picker.

## Development

```bash
uv run pytest            # run all tests (no network calls)
uv run pytest -k ontology
uv add <package>         # add a runtime dependency
uv add --dev <package>   # add a dev dependency
```

### Six Degrees investigation (offline, no LLM)

These need the people graph `data/wikidata/people_graph.sqlite` (see `docs/wikidata-ingestion-decision.md`), or set `PEOPLE_GRAPH_PATH` to point at another build.

```bash
uv run python scripts/investigate.py --search "Marie Curie"          # find QIDs
uv run python scripts/investigate.py Q7186 Q37463                    # deterministic run: timeline + paths
uv run python scripts/investigate.py Q7186 Q37463 --record my-case   # write fixture + replay trace
uv run python scripts/investigate.py Q7186 Q37463 --agent            # LLM agent (needs OPENROUTER_API_KEY)
uv run python scripts/benchmark_investigations.py --pairs 30         # recall vs. the complete graph
```

**Workbench:** run `uv run python -m src.server` and open http://localhost:5000/. Pick two people, choose deterministic or AI agent (or a recorded replay), and watch the graph grow. It shows the strongest path (the shortest is computed but not shown), a timeline, a Turtle source that highlights in both directions, and a sandbox for hypothetical edges. The legacy retail assistant moved to `/legacy`.

**Before a workshop:** run `uv run python -m src.preflight --online` and follow [`docs/workshop-runbook.md`](docs/workshop-runbook.md) (setup, the click-by-click demo flow, and what to do when something fails).

The Flask server exposes the investigation API under `/api/…`:

- `GET /api/health` and `GET /api/people/search?q=`
- `POST /api/investigations` with `{"source_qid", "target_qid", "mode"}`, where `mode` is `"deterministic"` (the default) or `"agent"`; or with `{"mode": "replay", "case_id"}`
- Without `OPENROUTER_API_KEY`, agent mode returns `503 agent_unavailable`. Use the deterministic mode, which is labelled as such in every event.
- `GET /api/investigations/<id>/events` (Server-Sent Events), plus `/graph`, `/paths` and `/turtle`

Replay needs only the committed files in `data/fixtures/`, not the people graph.

Third-party frontend libraries and their licenses are listed in [`docs/third-party-licenses.md`](docs/third-party-licenses.md).

## Project layout

```text
.
├── CLAUDE.md                 # contributor/agent guidance
├── IMPLEMENTATION_PLAN.md    # Six Degrees of Knowledge specification
├── .env.example
├── data/
│   └── mock_data.ttl         # legacy retail data
├── docs/
│   └── third-party-licenses.md
├── graph-view/
│   ├── live-editor.html
│   └── visualize.html        # legacy viewer (loads ../src/ui/vendor/vis-network.min.js)
├── ontology/
│   ├── people_connections.ttl  # teaching ontology of the people graph (Ontology view)
│   ├── ontology.owl.ttl      # legacy retail ontology
│   └── your_own_ontology.owl.ttl
├── src/
│   ├── ontology_agent.py
│   ├── server.py
│   └── ui/                   # workbench: index.html, js/ (ES modules), css/, vendor/, legacy/retail.html
├── tests/
├── pyproject.toml
└── uv.lock
```
