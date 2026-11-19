# KG Workshop: Connection Finder

An agentic connection explorer built for the workshop *Knowledge Graphs and Knowledge Graphs for Agentic AI*. Pick two notable people. An LLM agent explores a graph of [Wikidata](https://www.wikidata.org/) relationships, deciding which entity to expand next. Deterministic code does the graph queries, path search, ranking and validation. The result is the **strongest path** between the two people. Every hop is a Wikidata statement you can click to see its exact Turtle triple.

The demo shows the "unknown number of hops" question, the same shape as finding a fraud ring. Nobody knows in advance whether two people are 2 or 6 hops apart, and the connection exists only as a chain of statements. [`docs/workshop-context.md`](docs/workshop-context.md) explains what the demo is meant to teach.

There are three ways to run it, each needing more than the last:

| Level | What you can do | What you need |
|---|---|---|
| **1. Replays** | Step through the recorded demo runs | This repo only (no data, no API key) |
| **2. Live, no LLM** | Search between any two people with the deterministic baseline | + the people graph, built from a Wikidata dump (~115 GB free disk, a few hours) |
| **3. Live LLM agent** | Let an LLM agent drive the search | + an API key (DeepSeek or OpenRouter) |

## 1. Replay the recorded demos (no data needed)

The recorded runs are in the repo: `config/demo_cases.yaml` lists them, and `data/fixtures/` holds each run's events and the part of the graph it explored. Replaying needs neither the people graph nor an API key.

**Step 1. Install the tools.** You need [git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/getting-started/installation/), the Python package manager. uv also installs the right Python (3.13) by itself.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```
```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Step 2. Get the code and its dependencies.**

```bash
git clone https://github.com/martelosso/kg-find-connection.git
cd kg-find-connection
uv sync
```

**Step 3. Start the server.**

```bash
uv run python -m src.server
```

It prints the address to open, `http://localhost:5000/` by default.

> **macOS:** the AirPlay Receiver already uses port 5000, and the browser then shows "Access to 127.0.0.1 was denied" (HTTP 403). Use another port: `PORT=5050 uv run python -m src.server`, then open http://localhost:5050. To make it permanent, put `PORT=5050` in a `.env` file (see step 3 of [level 3](#3-live-llm-agent-add-an-api-key)). Or turn off System Settings → General → AirDrop & Handoff → AirPlay Receiver.

**Step 4. Play a replay.** Open the address in a browser (Chrome works best), then:

1. Choose a case in **Demo replays…** at the top right, for example *Lula and Snoop Dogg (AI agent)*.
2. Step through it with **Next** / **Back**, or the → and ← keys. Each step card says in plain words what happened and why.
3. The badge reads **REPLAY** with the time of the recording. A replay is never shown as live.

Things to try along the way:

- Click a graph edge to see its Wikidata statement.
- Open the **Source** pane (left) to see the Turtle for what is on screen. Selecting a line highlights it in the graph.
- Switch **Data | Ontology** to see the small ontology that the agent's tools check against.
- Open **History** (right) for the timeline and the final path.

Without the people graph, a live **Explore** works only between the recorded pairs, answered from their recorded data. Other pairs stop with a missing-data message. That is expected at this level.

Entity photos come live from Wikidata and Wikimedia Commons and need internet. Offline, entities keep their flat icons. Set `WIKIDATA_IMAGES=0` in `.env` to turn the lookups off.

## 2. Live searches: build the people graph from Wikidata

Live searches run on the **people graph**, an SQLite file (`data/wikidata/people_graph.sqlite`, about 2.5 GB) built from the public Wikidata "truthy" dump. It holds about 10 million entities (8.6 million people) and 27 million statements. It is not in git: it is too big, and anyone can rebuild it with the steps below.

If you have it on another machine already, just copy `people_graph.sqlite` into `data/wikidata/` and skip to step 4.

**What you need**

- **Disk:** about 115 GB free during the build: the 72 GB dump, a 38 GB intermediate slice and the 2.5 GB result. Afterwards you can delete the dump and the slice, so only the 2.5 GB graph is left (plus a 0.4 GB speed index created next to it).
- **Time:** downloading takes hours (Wikimedia serves it at roughly 5 MB/s, so 4–6 h). Processing takes about 20 minutes on a 16-core laptop with an SSD, longer on fewer cores.
- **Memory:** 16 GB of RAM is comfortable.
- **A bash shell** with curl, gzip, grep and perl: Linux, macOS, WSL, or Git Bash on Windows (it comes with [Git for Windows](https://git-scm.com/downloads)). Run the commands below in it, from the repo folder.

**Step 1. Choose a dump date.** Wikimedia keeps only recent dumps. Open https://dumps.wikimedia.org/wikidatawiki/entities/ and pick a recent dated folder (`YYYYMMDD`) that contains `wikidata-YYYYMMDD-truthy-BETA.nt.gz`. The recorded demos were made on `20261104`; any later date works too, and the snapshot date is shown in the app.

```bash
DATE=20261104          # or a newer date from the listing
```

**Step 2. Download the dump** (resumable: if it stops, run the same command again; it checks the MD5 at the end).

```bash
scripts/download_wikidata_dump.sh "$DATE"
# → data/wikidata/raw/wikidata-$DATE-truthy-BETA.nt.gz  (+ .meta.json with the date, size and MD5)
```

Wikimedia asks automated clients to identify themselves. Set `WIKIDATA_USER_AGENT="your-project/1.0 (your contact URL or email)"` before the command to send your own User-Agent ([policy](https://meta.wikimedia.org/wiki/User-Agent_policy)).

**Step 3. Extract and build.** Extraction streams the compressed dump and keeps only what the people graph needs: who is a human, the 26 allowed relationship properties, and English/Spanish labels. The dump is never decompressed to disk.

```bash
scripts/extract_people_slice.sh "data/wikidata/raw/wikidata-$DATE-truthy-BETA.nt.gz"
# → data/wikidata/slice/slice.nt  (~38 GB, ~260M lines)

uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
    --out data/wikidata/people_graph.sqlite \
    --dump-meta "data/wikidata/raw/wikidata-$DATE-truthy-BETA.nt.gz.meta.json"
# → data/wikidata/people_graph.sqlite  (+ people_graph.sqlite.adjacency/, a speed index)
```

Pass `--dump-meta`: it records the snapshot date that every result shows. Once the build has finished, you can delete `data/wikidata/raw/` and `data/wikidata/slice/`.

**Step 4. Check and run.**

```bash
uv run python -m src.preflight      # READY / NOT READY, with the reason for each check
uv run python -m src.server
```

In the browser, type two names in the people pickers, keep **Baseline** selected (no LLM), and click **Explore**. To keep the graph somewhere else, set `PEOPLE_GRAPH_PATH` to its path in `.env`.

[`docs/wikidata-ingestion-decision.md`](docs/wikidata-ingestion-decision.md) explains why the data comes from an offline dump, which properties are kept, and the measurements behind the hub caps.

## 3. Live LLM agent: add an API key

The agent uses any OpenAI-compatible chat API. DeepSeek and OpenRouter are supported.

```bash
cp .env.example .env
# then edit .env and set DEEPSEEK_API_KEY=… or OPENROUTER_API_KEY=…
```

Restart the server, pick two people, choose **LLM agent** and click **Explore**. The agent needs the people graph from level 2 too. Without a key, the **LLM agent** button is disabled, and Baseline and replays still work. LLM runs are not deterministic: the same pair can take different routes on different runs.

`.env` is gitignored. Never commit it or share your key. Its settings:

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY` | Key for the LLM agent. `LLM_PROVIDER` (`deepseek` or `openrouter`) picks one when both are set. |
| `DEEPSEEK_MODEL` / `OPENROUTER_MODEL` | Optional model overrides. |
| `DEEPSEEK_THINKING` | `disabled` (default, 1–3 s per agent turn) or `enabled` (slower, finds harder pairs). |
| `PORT` | Server port (default 5000). |
| `PEOPLE_GRAPH_PATH` | People graph somewhere other than `data/wikidata/people_graph.sqlite`. |
| `WIKIDATA_USER_AGENT` | Your User-Agent for Wikidata requests (dump download, entity photos). |
| `WIKIDATA_IMAGES` | `0` turns the live photo lookups off. |

## Recording your own replay

After a live run that found a path, the result card offers **Save as demo replay**. It writes a fixture and an event trace to `data/fixtures/` and adds the case to `config/demo_cases.yaml`. It refuses unless the recording rebuilds exactly the same graph and paths. From the command line:

```bash
uv run python scripts/investigate.py --search "Marie Curie"                  # find QIDs
uv run python scripts/investigate.py Q7186 Q37463                            # baseline run: timeline + paths
uv run python scripts/investigate.py Q7186 Q37463 --agent                    # LLM agent run
uv run python scripts/investigate.py Q7186 Q37463 --record my-case           # record a replay
```

Presenting at a workshop? Run `uv run python -m src.preflight --online` until it says **READY**, then follow [`docs/workshop-runbook.md`](docs/workshop-runbook.md) (the click-by-click flow, what to say, and what to do when something fails).

## How it works

- **Data:** an offline people graph from the Wikidata truthy dump. Statements keep their Wikidata direction; a shared employer, school or film is kept as a real node (an association, not proof of contact). Very large hubs (big cities, huge organisations) may end a path but never sit in the middle of one.
- **Policy:** [`config/relationships.yaml`](config/relationships.yaml) says which relations count and how strong they are (family and teacher/student links count most). [`ontology/people_connections.ttl`](ontology/people_connections.ttl) gives their meaning; it is checked against the policy, and any disagreement is an error.
- **Search:** both people's sides grow until they meet. The strongest path is the cheapest one under the policy, found with an exact hop-limited search. Results are always "within the explored subgraph", never claimed as globally optimal.
- **Agent:** one tool-calling loop. The agent chooses what to expand; its tools enforce the policy in code, and the final answer may only name entities on a verified path (the grounding check).
- **Budgets:** [`config/search.yaml`](config/search.yaml) caps hops, explored entities, tool calls and wall time. Hitting a cap ends with an honest partial result.

[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) is the full specification; [`CLAUDE.md`](CLAUDE.md) has the detailed engineering notes.

## Development

```bash
uv run pytest            # all tests; no network calls
uv run pytest -k paths   # a subset
uv add <package>         # add a dependency (never pip install)
```

The UI tests run small JavaScript modules in [Node.js](https://nodejs.org/); install it to run them.

## Project layout

```text
config/          relationships.yaml (policy), search.yaml (budgets), demo_cases.yaml (replays), demo_seeds.txt
data/fixtures/   recorded demo runs (committed); data/wikidata/ (people graph, dump, caches) is gitignored
docs/            workshop context and runbook, ingestion decision, search tuning, visual design
ontology/        people_connections.ttl (the teaching ontology); the retail files are legacy examples
scripts/         dump download, extraction and build; investigate.py; demo-pair and benchmark tools
src/             server.py (Flask + API), graph/, investigations/, agent/, wikidata/, ui/ (vanilla JS workbench)
tests/
```

### Legacy examples

The repository started as a small knowledge-graph playground; those files still work:

- `graph-view/live-editor.html`: a standalone live Turtle editor with a graph view. Open it in a browser and load a `.ttl` file.
- `ontology/ontology.owl.ttl` and `data/mock_data.ttl`: a hand-written supermarket ontology and fictional data. `uv run python -m src.ontology_agent "What classes exist for pricing?"` asks questions about it through OpenRouter. The server also serves the matching chat UI at `/legacy`.

## Data and licenses

- **The code** is under the [MIT License](LICENSE).
- **Wikidata** content is [CC0](https://creativecommons.org/publicdomain/zero/1.0/). A statement means "Wikidata records this", not a verified fact.
- **Entity photos** are loaded at view time from Wikimedia Commons, under each file's own license. The detail box of a clicked entity links to the file page with its author and license. No photos are stored in this repo.
- **Vendored frontend libraries** (CodeMirror, vis-network, Nunito Sans) keep their licenses: see [`docs/third-party-licenses.md`](docs/third-party-licenses.md).
