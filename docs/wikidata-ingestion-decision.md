# Wikidata ingestion: decision record

**Status:** decided (2026-11-12). Full-build measurements are pending until the download finishes.
**Decision:** the primary provider is an **offline "people graph"** built once from the official Wikidata truthy N-Triples dump and stored in SQLite. The demo makes **no live Wikidata calls** at runtime. Live SPARQL/REST access is optional, possibly later, and only for pairs outside the prepared set.

## Why

The workshop needs to be reliable live. The options considered (from `IMPLEMENTATION_PLAN.md` §4):

| Option | Verdict |
|---|---|
| A. `Special:EntityData/<QID>.ttl` per entity | Outgoing edges only, so incoming links ("who else studied here?") need another mechanism. Depends on the network at demo time. |
| B. WDQS SPARQL, live | The only live way to get incoming links. It has a ~60 s query timeout and per-client time budgets (`429`). Large hubs (e.g. Harvard) are expensive. Depends on the network. |
| C. REST/Action API | Good for search and lookup, but outgoing only and returns JSON, which would need converting. |
| D1. Small per-pair snapshot (SPARQL `CONSTRUCT`) | Works offline, but only for prepared pairs, and the graph is partial, so results can only say "among explored". |
| **D2. Filtered people graph from the full dump** | **Chosen.** Offline, deterministic and dated. Incoming links are just an index lookup. Shortest and strongest can be **complete within the configured scope** (`scope: "complete_configured_scope"`). |

D2 also settles open decisions 1 and 2 in plan §13 (how to get RDF, and how to get incoming links).

## Source

- File: `wikidata-20261104-truthy-BETA.nt.gz` from `https://dumps.wikimedia.org/wikidatawiki/entities/20261104/`. Same file as `latest-truthy.nt.gz` (same ETag), last modified 2026-11-06.
- Size: 71,837,406,207 bytes. MD5 `3057f5436b8a35f6e2e81b8ebcda0c59` (from the dump's `md5sums.txt`).
- Contents: "truthy" statements, meaning best-rank direct values with no qualifiers or references. This matches the plan's default projection (§3.4). Statement IDs and qualifiers are **not** available from this source. Edge provenance is therefore the dump file and date plus the Wikidata entity and property URLs.

## Pipeline (reproducible)

```bash
bash scripts/download_wikidata_dump.sh 20261104
#   -> data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz (+ .meta.json); resumable, MD5-verified
bash scripts/extract_people_slice.sh data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz
#   -> data/wikidata/slice/slice.nt: P31 + allowlisted wdt: properties + en/es rdfs:label
uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
    --out data/wikidata/people_graph.sqlite \
    --dump-meta data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz.meta.json
```

Everything under `data/wikidata/` is gitignored. Commit curated fixtures in `data/fixtures/`.

**Extraction scope:** `P31` plus `P26 P22 P25 P3373 P40 P451 P1038 P1066` (interpersonal) plus `P108 P69 P463 P54` (institutional) plus `P39` (position held), with `rdfs:label` in `en` and `es`.

*Scope v2 (2026-11-13, policy version 2):* added **P40 child**, **P451 unmarried partner**, **P1038 relative** (family group; relative is weaker because its kinship type is a qualifier the truthy dump omits) and **P54 member of sports team** (institutional, so its teams are organisations under the hub cap). Reason: the demo seeds now include people everyone knows today (footballers, musicians, influencers), who connect mostly through teams, partners and children; without these they have almost no edges. P40 also catches children recorded only on the parent's page. The first full extraction (v1 scope: 248,380,336 lines, 33.8 GB, 29 min, reproduced byte-for-byte by a second run) was therefore redone with the v2 scope. A test keeps `extract_people_slice.sh`, `build_people_graph.py` and the policy on the same property list. Only statements whose **subject is a human** (P31 = Q5) are kept. Adding a property means re-running extraction. Eligibility and ranking policy stay in `config/relationships.yaml` (Milestone 2), not in the extraction scripts.

**P39 "position held"** is extracted but **off by default**. It belongs to a "weak association" group the presenter can enable, because it links people through role nodes (e.g. "member of the House of Commons of Canada") rather than organizations. It is about 25% of all edges.

## Evidence: 2 GiB sampling run (first 2,147,483,648 bytes of the dump, 2026-11-12)

- Download speed **~4.6 MB/s**, so the full 71.8 GB takes about 4–6 h. The network is the bottleneck. grep handled ~500k triples/s.
- The sample contained 238.6M triples and 2.43M entities with P31, of which 394k were humans.
- Allowlisted statements with a human subject: 531k in the sample. A naive ×33.5 projection gives **~18M edges** (probably an overestimate, because the start of the dump is biased toward older QIDs).

| PID | Sample (human subject) | Naive projection |
|---|---|---|
| P69 educated at | 157,613 | ~5.3M |
| P39 position held | 136,025 | ~4.6M |
| P108 employer | 62,667 | ~2.1M |
| P3373 sibling | 44,664 | ~1.5M |
| P463 member of | 42,549 | ~1.4M |
| P22 father | 32,916 | ~1.1M |
| P26 spouse | 30,684 | ~1.0M |
| P25 mother | 15,933 | ~0.5M |
| P1066 student of | 7,843 | ~0.26M |

Findings that drive the design:
1. **Hubs dominate.** Harvard (Q13371) is the top object for both P69 and P108 even in a 3% sample. In the full graph, unrestricted shortest paths will mostly go "both studied at a big university". A hub policy (threshold or penalty from the measured degree distribution) is required. "Complete" means *complete under that policy*, and the UI must say so.
2. **Interpersonal edges are sparse** (~5% of edges). Most person-to-person paths go through institutions, so demo pairs that show strong interpersonal paths must be picked on purpose.
3. **The dump isn't sorted by QID.** Labels and P31 for an entity can appear anywhere, so the build loads everything and then joins. A single-pass streaming join isn't possible.
4. Extraction shrinks the data a lot. The sample went 2 GiB gz → 742 MB slice (6.1M lines) → 42 MB SQLite in about 20 s. Full projection: about 25 GB slice and about 1–2 GB SQLite.
5. Four unparseable lines were blank-node `owl:onProperty` restrictions, which are ignored. Labels decode correctly, including accents and escapes.

## Full build results

_Pending. Record: extraction time, slice size, build time, DB size, final counts per PID and kind, unlabeled-entity count, degree distribution (P50/P90/P99/max in-degree per institutional PID), and connectivity checks for the candidate demo pairs._

## Limitations and fallback

- Data is frozen at the dump date. The UI must show "Wikidata snapshot 2026-11-04".
- The truthy projection has no qualifiers (start and end dates), so the graph can't say "worked there at the same time". Institutional links stay **associations**.
- The subject must be human, so organization→organization links (e.g. a university that is a member of a consortium) are excluded by design.
- Fallback if the full build fails or is too large: build per-pair snapshots (option D1) with bounded WDQS `CONSTRUCT` queries, using the same SQLite schema.

## Open follow-ups

- Runtime pathfinding store: SQLite with neighbor queries, or SQLite plus an in-memory compact adjacency (CSR) for complete bidirectional search. Decide in Milestone 2 using the measured size.
- Hub threshold and `hub_penalty` values: set from the measured degree distribution.
- Whether to also keep `P31`-based `kind` refinement, e.g. university vs. company vs. academy, for display.
