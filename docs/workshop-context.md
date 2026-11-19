# Workshop context: who the demo is for and what it has to prove

This file records the session that *KG Workshop: Connection Finder* is built for. Use it to judge design choices: a feature earns its place if it helps the audience below take away the points below. For the click-by-click flow see [`workshop-runbook.md`](workshop-runbook.md); for the full specification see `IMPLEMENTATION_PLAN.md`.

## The workshop

**Title:** Knowledge Graphs and Knowledge Graphs for Agentic AI

**Overview:** A practical session on when knowledge graphs are worth it and how AI agents use them. The first part uses fraud investigation as one running example. It covers how knowledge graphs and ontologies represent information, a simple test for when a graph beats a relational database (and when it doesn't), and how much "graph" a problem actually needs. Those levels range from writing down an ontology to running a dedicated graph database. The second part is a live demo: an AI agent navigates relationships in a knowledge graph to find insights that no single data record contains.

**Main goal:** Give attendees a clear way to decide whether a knowledge graph is worth building for their own problems, and show the practical value of knowledge graphs in agentic AI. The live demo makes this concrete: an AI agent explores Wikidata to find and evaluate connections between seemingly unrelated famous figures. This is the classic "unknown number of hops" question, the same shape as finding a fraud ring.

**Key takeaways.** Attendees will be able to:

1. Apply a two-question test to a problem: do you know in advance how many joins the question needs? Is the answer a property of one record, or a pattern across records?
2. Recognize problems that look like graph problems but aren't, such as fixed short lookups, aggregates and dashboards.
3. Pick the right level of investment, and explain why an ontology with a clear owner is often the first and cheapest win.
4. Understand how a shared ontology gives agents business context they can check against, and how agents traverse typed relationships to reason across connected data.

**Audience:** A mixed group of data scientists, AI/ML engineers, software developers and technology enthusiasts. No prior experience with knowledge graphs is required.

## Where the demo sits

The demo is **part two**. By then the audience has heard the fraud example, the two-question test and the levels of investment. The demo does not re-teach those; it shows them working on real data. It is the payoff for takeaway 4 and a live answer to "yes" on both questions of takeaway 1.

## How each takeaway shows up in the demo

| Takeaway | What the audience sees |
|---|---|
| **1. Unknown number of joins** | Nobody knows in advance whether Lula and Snoop Dogg are 2 or 6 hops apart. The graph grows from both people until the two sides meet. A SQL query would need a fixed number of self-joins; here the hop count is the *answer*, not an input. |
| **1. Pattern across records** | No single Wikidata entity says "Lula is connected to Snoop Dogg". The connection exists only as a chain of statements (film → director → film → actor → film), the same way a fraud ring exists only as shared accounts, devices and addresses. |
| **2. Not every problem is a graph problem** | The parts that are *not* graph problems are done with plain code: resolving a name to a QID is an indexed lookup, budgets are counters, the ranking is arithmetic. The graph earns its place only in the traversal. |
| **3. Levels of investment** | The data is a SQLite file plus an in-memory adjacency index built from a Wikidata dump. No graph database. What makes it a knowledge graph is the typed properties and the policy in `config/relationships.yaml`, a small, owned, versioned description of which relations count and how strong they are. That file is the "ontology with a clear owner" in miniature. |
| **4. Ontology as context the agent checks against** | The agent sees relation *groups* (family, mentorship, institutional, works, birthplace) and their meaning, not raw data. Tools enforce the policy in code: disabled groups, hubs and unexplored entities are refused. The grounding check rejects any final answer that names an entity not on a verified path. |
| **4. Agents traverse typed relationships** | Each agent turn is a visible decision ("expand Oliver Stone, director links are promising") followed by tool calls, and every edge is a Wikidata statement the presenter can click to see its exact Turtle triple. The deterministic baseline runs the same tools without an LLM, so the audience can see what the model adds and what it does not. |

**The fraud parallel to say out loud:** famous person ↔ suspicious account; film, school or employer ↔ shared device, address or bank account; "strongest path" ↔ the most convincing chain of evidence; hub (Harvard, a big city) ↔ a public Wi-Fi network that everyone shares and that proves nothing.

## What the audience implies for the build

These decisions follow from the audience and are already in the code (details in `CLAUDE.md`):

- **No KG background assumed.** All on-screen text uses the field's terms (entity, property, statement, hop, frontier, hub, tool call, grounding check) and explains them the first time they appear. Never "backend" on screen. Glossary: runbook, "Vocabulary".
- **One answer, not two.** Showing both shortest and strongest paths confused people, so the UI shows the strongest path only.
- **Step-through, not autoplay.** The presenter advances with Next/Back; each step card says in plain words what happened, why the agent chose it, and whether the tools suggested it or the agent picked it itself (credit where due).
- **The fruitless tail is folded.** After the strongest path stops changing, the remaining search is one "nothing stronger turned up" step, so the story stays short.
- **Honesty is part of the lesson.** Results are "within the explored subgraph"; a shared organisation is an association, not proof of contact; a statement is what Wikidata records, not verified fact; REPLAY is never shown as LIVE. These map directly to the caution an investigator needs with a fraud graph.
- **It must work offline.** A conference room network can fail, so the people graph is local, demo cases are recorded replays, and the baseline works without an LLM key.
- **Recognisable people.** Demo pairs mix historical figures with recent celebrities from Brazil, Colombia and elsewhere so a mixed room knows at least one name in each pair (`config/demo_seeds.txt`).
