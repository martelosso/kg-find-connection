# knowledge-graph-workshop

A playground for learning how knowledge graphs and ontologies work. It includes a graph visualizer, a live Turtle editor and an LLM-powered assistant.

## Status

This repository is being extended into **Six Degrees of Knowledge**, an agentic explorer that connects two notable people through Wikidata relationships. See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the specification and [`CLAUDE.md`](CLAUDE.md) for contributor guidance.

- **Milestone 0 (baseline) is complete:** dependencies, test suite, an offline live editor and `.env.example`.
- The retail/supermarket assets below are **legacy examples**. They still work, but new features are not built on them.

## Project contents

| Path | What it is |
|---|---|
| [`graph-view/live-editor.html`](graph-view/live-editor.html) | Standalone live Turtle editor (CodeMirror 5) with a vis-network graph and two-way highlighting. It works offline, using the libraries vendored in `src/ui/vendor/`. The workbench is built on its parser, theme and highlighting. |
| [`graph-view/visualize.html`](graph-view/visualize.html) | Older standalone Turtle graph visualizer (legacy). |
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

- `OPENROUTER_API_KEY`: needed for the LLM assistant.
- `OPENROUTER_MODEL`: optional model override.
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
