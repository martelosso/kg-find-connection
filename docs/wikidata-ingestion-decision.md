# Wikidata ingestion: decision record

**Status:** decided (2026-11-12). Full graph built and measured on 2026-11-13 (see "Full build results").
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
#   -> data/wikidata/slice/slice.nt: P31 + allowlisted wdt: properties + en/es/mul rdfs:label
uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
    --out data/wikidata/people_graph.sqlite \
    --dump-meta data/wikidata/raw/wikidata-20261104-truthy-BETA.nt.gz.meta.json
```

Everything under `data/wikidata/` is gitignored. Commit curated fixtures in `data/fixtures/`.

**Extraction scope (v3):** `P31`; family `P26 P22 P25 P3373 P40 P451 P1038`; mentorship `P1066 P802 P184 P185`; institutional `P108 P69 P463 P54 P112`; works `P161 P57 P58 P86 P175`; weak `P39 P19 P166 P102 P737`; with `rdfs:label` in `en`, `es` and `mul` (see "Labels: the mul fallback"). Statements are kept when the subject is a human, or, for the works properties and P112, when the object is a human.

*Scope v2 (2026-11-13, policy version 2):* added **P40 child**, **P451 unmarried partner**, **P1038 relative** (family group; relative is weaker because its kinship type is a qualifier the truthy dump omits) and **P54 member of sports team** (institutional, so its teams are organisations under the hub cap). Reason: the demo seeds now include people everyone knows today (footballers, musicians, influencers), who connect mostly through teams, partners and children; without these they have almost no edges. P40 also catches children recorded only on the parent's page. The first full extraction (v1 scope: 248,380,336 lines, 33.8 GB, 29 min, reproduced byte-for-byte by a second run) was therefore redone with the v2 scope. A test keeps `extract_people_slice.sh`, `build_people_graph.py` and the policy on the same property list.

*Scope v3 (2026-11-13, policy version 3):* a second review found that the biggest gap was the **human-subject rule**, not a missing property: film casts, song credits, directors and founders are recorded on the film, song or organisation and point at the person, so they were all dropped. Measured on the first 20 GB of the decompressed dump (raw statement counts, biased toward older QIDs): P161 cast member 129k (about as common as P69), P175 performer 36k, P57 director 22k, P58 screenwriter 16k, P86 composer 15k, P185 doctoral student 14k (P1066 only 6.6k), P802 student 6.5k, P112 founded by 7k, P184 doctoral advisor 3.6k; weak: P19 place of birth 207k, P166 award received 132k, P102 political party 35k, P737 influenced by 2.5k.

- **Works group (on by default, strength 0.5):** P161, P57, P58, P86, P175, kept when the **object** is a human (`OBJECT_SIDE_PIDS` in the builder). Works become nodes of kind `work`; a shared credit is an association (likely, not proven, contact).
- **Mentorship:** P802 student (the teacher's side of P1066), P184 doctoral advisor, P185 doctoral student. Institutional: P112 founded by (organisation → person, also object-side).
- **Weak groups, off by default:** birthplace (P19, places get kind `place`), awards (P166), party (P102, parties are organisations), influence (P737). Extracting them costs nothing extra; leaving them out would have meant another re-extraction to try them. *Policy v4 turned birthplace on by default, with its own hub cap; see "Hub policy and birthplace" below.*
- **Not extracted:** P50 author (dominated by scholarly-article co-authorship: tens of millions of statements), P1344 participant in (huge, vague event hubs), and the tiny P1327, P3448, P169. Only statements whose **subject is a human** (P31 = Q5) are kept. Adding a property means re-running extraction. Eligibility and ranking policy stay in `config/relationships.yaml` (Milestone 2), not in the extraction scripts.

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

## Full build results (2026-11-13, extraction scope v3, policy v4)

The full people graph exists at `data/wikidata/people_graph.sqlite`. From the 72 GB dump to a ready graph took about 19 minutes on the workshop laptop (16 logical cores, NVMe SSD).

| Step | Time | Output |
|---|---|---|
| Extraction (`extract_people_slice.sh`, 16 jobs) | 766 s | `slice.nt`: 263,847,647 lines, 35.8 GB |
| Build (`build_people_graph.py`, 8 workers) | 294 s | `people_graph.sqlite`: 2.45 GB |
| Adjacency index (end of build) | 42 s | `people_graph.sqlite.adjacency/`: 424 MiB |

Load stats: 125.5M P31 lines, 28.3M relationship statements, 109.9M en/es labels, 27 unparsable lines (blank-node restrictions, as in the sample) and 75k skipped lines.

**Graph:** 10,470,079 entities, 26,649,326 edges. 1,135,041 entities (11%) had neither an English nor a Spanish label; with the `mul` fallback (below) 549,690 (5%).

| Kind | Entities |
|---|---|
| person | 8,606,664 |
| work | 949,509 |
| organization | 413,365 |
| place | 302,889 |
| position | 148,820 |
| other | 48,832 |

| Edges per property | | | |
|---|---|---|---|
| P19 place of birth | 4,379,125 | P25 mother | 768,248 |
| P69 educated at | 3,477,364 | P102 political party | 656,269 |
| P108 employer | 2,412,220 | P3373 sibling | 607,201 |
| P40 child | 1,999,398 | P463 member of | 555,494 |
| P161 cast member | 1,812,807 | P57 director | 464,543 |
| P166 award received | 1,780,673 | P184 doctoral advisor | 407,746 |
| P54 sports team | 1,657,540 | P185 doctoral student | 400,154 |
| P39 position held | 1,656,484 | P175 performer | 394,979 |
| P22 father | 1,234,058 | P86 composer | 288,916 |
| P26 spouse | 950,032 | P58 screenwriter | 250,221 |
| P1066 student of | 135,315 | P1038 relative | 116,834 |
| P112 founded by | 109,881 | P802 student | 90,380 |
| P737 influenced by | 23,429 | P451 unmarried partner | 20,015 |

The groups that are off by default (position, awards, party, influence) hold about 4.1M edges.

**Non-person degree distribution** (degree = all extracted links, as the hub rule uses), from `scripts/analyze_hubs.py`:

| Kind | p50 | p90 | p99 | p99.9 | p99.99 | max | > 1,000 | > 5,000 |
|---|---|---|---|---|---|---|---|---|
| organization | 2 | 19 | 380 | 3,454 | 14,370 | 42,296 | 1,539 | 239 |
| work | 1 | 9 | 26 | 62 | 201 | 1,161 | 6 | 0 |
| position | 2 | 11 | 96 | 1,151 | 6,703 | 17,457 | 175 | 24 |
| place | 2 | 11 | 138 | 820 | 3,049 | 19,348 | 224 | 15 |
| other | 3 | 37 | 402 | 3,538 | 14,602 | 28,574 | 205 | 27 |

Birthplaces on their own (P19 edges per place): 306,805 places, p50 2, p90 12, p99 178, max 42,278 (Berlin; then Vienna, Prague, Paris, Moscow, New York City, London and Tokyo, all above 22k). 4,346,308 people (about half) have a recorded birthplace.

### Hub policy and birthplace (policy v4)

Measured with exact searches on 100 demo seed pairs (`config/demo_seeds.txt`, recent × historical, seed 5), global hub cap 5,000:

| Setting | Connected | Searches cut off by work caps | Shortest via birthplace | Strongest via birthplace | Median / max time |
|---|---|---|---|---|---|
| Birthplace off (v3) | 80% | 3 | 0% | 0% | 4.5 s / 61 s |
| Birthplace on, no place cap | 91% | 11 | 29% | 13% | 6.7 s / 76 s |
| Place cap 2,000 | 90% | 10 | 24% | 11% | 6.4 s / 69 s |
| **Place cap 1,000 (chosen)** | **89%** | **11** | **24%** | **10%** | **6.3 s / 73 s** |
| Place cap 500 | 90% | 11 | 22% | 10% | 6.5 s / 73 s |
| Place cap 250 | 89% | 10 | 20% | 9% | 6.2 s / 66 s |

Decisions:
- **Birthplace is on by default** (policy v4). It lifts connectivity on the seed pairs from 80% to about 90%. Its strength (0.10) keeps it out of strongest paths unless nothing better connects the pair.
- **Places have their own hub cap of 1,000** (`pathfinding.max_intermediate_degree_by_kind.place`). Only towns with at most 1,000 recorded births can be intermediate nodes; that keeps 62% of the birthplace edges. Between 250 and 2,000 the differences are small at 100 pairs, so 1,000 is a middle choice, not a sharp optimum.
- **The global cap stays 5,000 (provisional).** Organizations above it: 239.
- **Known cost:** with birthplace on, 10–11 of 100 exact searches hit the work caps (`max_visited_nodes` and `max_expanded_states`, 250,000) and return partial results, against 3 before. The extra edges hang on people, so the place cap does not fix this. Raising the work caps would make those searches exact at the price of slower worst cases. This affects the offline prep tools (`find_demo_pairs.py`, `analyze_hubs.py`), not live investigations, which search their small explored graph.

### Global hub cap sweep and the city-kind fix (policy v5)

Same 100 seed pairs (recent × historical, seed 5), place cap 1,000, v4 graph:

| Global cap | Connected | Strongest ≠ shortest | Cut off by work caps | Median / max time | Busiest hubs on shortest paths |
|---|---|---|---|---|---|
| 1,000 | 70% | 34% | 6 | 3.1 s / 78 s | none ≥ 1,000 |
| 2,000 | 83% | 35% | 12 | 5.6 s / 81 s | NYU Tisch, United Nations, Barnard (~1.3k) |
| 5,000 | 89% | 48% | 11 | 7.5 s / 106 s | Trinity College (4.7k) ×8, American Philosophical Society ×5 |
| 10,000 | 88% | 51% | 13 | 9.9 s / 110 s | American Academy of Arts and Sciences ×12, Royal Society ×6 |
| none | 92% | 43% | 8 | 12.8 s / 136 s | Cambridge (20.7k) ×10, Edinburgh, Los Angeles |

- **The sweep exposed a classification bug.** Kinds were assigned by precedence (organization > work > position > place), so a city with a single employer, school or team link became an `organization` and fell under the 5,000 cap instead of the place cap. 3,129 "organizations" were mostly birthplaces, holding 1.46M of the 4.35M P19 edges; 225 of them (Washington D.C., Bucharest, Melbourne, Heidelberg, "England", …) had 1,000–5,000 links and could connect people. This also explains why the v4 place-cap sweep barely moved between 250 and 2,000.
- **Fix (policy v5):** a node whose birthplace links are at least half of its links is a `place`, whatever else points at it. Requires a rebuild.
- **Provisional choice: keep the global cap at 5,000.** 2,000 → 5,000 gains 6 points of connectivity and many more strongest ≠ shortest pairs; 10,000 adds nothing and admits mega-academies. To be confirmed by re-running this sweep on the v5 graph.
- The work caps cut off ~11% of searches at every cap except 1,000; decide on them after the v5 re-run.

### Labels: the mul fallback (2026-11-13)

Seed resolution on the full graph could not find Marie Curie, Shakira, Beyoncé, Federer, Djokovic and 21 other seeds, and resolved Donald Trump and Pierre Curie to low-degree namesakes. The famous items were in the graph with all their edges but **no label**. The dump has no `@en` or `@es` label for them: their name is only in Wikidata's language-independent `mul` label (`"Marie Curie"@mul`), and since 2024 editors and bots remove en labels that duplicate it.

- Extraction now also keeps `@mul` labels (+20.8M slice lines, 35.8 → 38.3 GB; 1,119 s, with the test suite running at the same time).
- The builder stores `mul` per entity and fills `label_en` / `label_es` only where they are still empty, after the whole scan. An entity's lines can fall into different parallel chunks, so filling earlier could beat a real en label. Tested in `test_mul_label_fills_missing_en_and_es_only`.
- Wikidata's own language fallback also goes through `mul`, so the result matches what wikidata.org shows. Missing labels: 1,135,041 → 549,690. Edges and kinds are unchanged by this.
- After the rebuild, every seed resolves. Seven names still need a pinned QID in `config/demo_seeds.txt`: Jay-Z (label "JAŸ‐Z"), Queen Victoria ("Victoria"), Pope Leo XIV ("Leo XIV"), Jungkook ("Jung Kook"), Melinda French Gates ("Melinda Gates"), and the tennis players João Fonseca and Carlos Alcaraz (the exact names resolve to better-connected footballers).

## Extraction speed (2026-11-13)

The original extraction was one `gzip -dc | grep -F | awk` pipeline: 29 min for the v1 scope (1,760 s). Measured per GiB of compressed dump (about 13 GB of text): gzip 10.7 s, grep 21.8 s, awk 14.3 s. grep was the bottleneck (Git-for-Windows grep 3.0, ~0.5 GB/s whatever the pattern), and the pipeline used about 3 of 16 cores.

- **Parallel ranges.** The dump is about 1,200 concatenated gzip members (~58 MB compressed each). `scripts/gzip_members.py` finds a member start near each of N even cut points (0.4 s), and `extract_people_slice.sh` runs the same filter on each byte range in parallel (`JOBS`, default `nproc`), then concatenates the parts in order. Each line is filtered on its own, so the output is byte-identical to the sequential run. A cut that is not a real member boundary makes gzip fail (truncated member or CRC error) and the script aborts without writing; `tests/test_gzip_members.py` covers this.
- **perl instead of awk** for the en/es label filter: identical output, 2.7× faster.
- Result: on an 8 GB prefix, 196 s → 45–52 s with identical md5. The full v3 extraction took 766 s (27 properties, against 10 for the 29-minute v1 run).
- **Tried and rejected on Windows:**
  - **ISA-L (isal 1.9.1):** the command-line tool is 6× slower than gzip, and the library itself decompresses no faster than zlib on this machine (10.3 s vs 11.1 s per GB).
  - **ripgrep 15.2:** 25% faster than grep on a file, but pathologically slow reading from a Git-Bash pipe (120 s vs 1.3 s per GB). The `--pre` workaround gave identical output but was slower end to end (92 s vs 52 s). Both would probably help on Linux.

## Limitations and fallback

- Data is frozen at the dump date. The UI must show "Wikidata snapshot 2026-11-04".
- The truthy projection has no qualifiers (start and end dates), so the graph can't say "worked there at the same time". Institutional links stay **associations**.
- The subject must be human, so organization→organization links (e.g. a university that is a member of a consortium) are excluded by design.
- Fallback if the full build fails or is too large: build per-pair snapshots (option D1) with bounded WDQS `CONSTRUCT` queries, using the same SQLite schema.

## Open follow-ups

- ~~Runtime pathfinding store~~: decided, SQLite plus a memory-mapped CSR adjacency (`IndexedPeopleGraph`, 424 MiB on the full graph).
- Global hub cap (5,000) and `hub_penalty_weight`: still provisional. Run the cap sweep on the full graph (`analyze_hubs.py --seeds … --cross recent historical`) and set it before recording demo cases.
- Work caps (`max_visited_nodes`, `max_expanded_states`): decide whether to raise them now that birthplace is on (10–11% of seed-pair searches are cut off).
- Whether to also keep `P31`-based `kind` refinement, e.g. university vs. company vs. academy, for display.

## Build speed (2026-11-13)

`build_people_graph.py` originally loaded every slice line into raw SQLite tables (regex per line) and joined them. About 95% of the slice is P31 and labels for entities that never enter the graph (in a 20M-line prefix: 9.66M P31, 9.27M labels, 1.08M relationship edges). The builder now scans twice with byte-level parsing in parallel processes, filters in numpy, and loads only graph nodes' labels and types. Output is identical to the old builder (all tables compared on a 20M-line prefix, with 1 and 8 workers).

| Input | Old builder | New, 1 worker | New, 8 workers |
|---|---|---|---|
| 20M lines (2.7 GB) | 81 s | 50 s | 19 s |
| 60M lines (8.2 GB) | - | - | 61 s, ~2.5 GB peak RAM |
