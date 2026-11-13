# Search tuning (step 4, 2026-11-13): `config/search.yaml` v3

**Decision:** the deterministic frontier rewards well-connected nodes (`frontier.degree_weight` 0.15 → **−0.5**), and the exploration budgets grow: `max_explored_nodes` 600 → **2,000**, `max_explored_edges` 2,000 → **7,000**, `max_provider_requests` 30 → **40**. The agent's tool-call budget (30), the wall time (90 s) and `max_edges_per_expansion` (100) are unchanged.

## Why

The frontier scores a candidate as *route cost from its side's endpoint + `degree_weight` · ln(1 + degree)*, lower first. With a positive weight it preferred cheap, low-degree nodes. On real pairs those are mostly leaf relatives (a star's children, a politician's spouse), which are dead ends. Every famous pair that failed used the whole request budget on them while the actual bridges, film and song credits and well-connected people, waited.

## Evidence

Harness: the bounded deterministic investigation on the full v5 people graph, per variant, on the same pairs. Pairs:
- **Famous:** 355 pairs from `find_demo_pairs.py`, connected within 2–4 hops on the complete graph (239 international × Brazil seeds, 116 recent × historical seeds).
- **Random:** 60 random connected pairs from `benchmark_investigations.py --pairs 60 --seed 7`, mostly obscure people 5–6 hops apart.

"Found" means the live run found any path. "Optimal" means that path has the exact shortest hop count.

| degree_weight | nodes / edges / requests | Famous found | Optimal | Random found | Time p90 / max |
|---|---|---|---|---|---|
| +0.15 (v2) | 600 / 2,000 / 30 | 33 (9%) | 30 | 6 | — / 1.4 s |
| 0 | 600 / 2,000 / 30 | 57 | 49 | — | — / 2.9 s |
| −0.3 | 600 / 2,000 / 30 | 79 | 62 | 7 | — / 4.1 s |
| −0.3 | 1,500 / 5,000 / 30 | 134 | 93 | 11 | — / 3.6 s |
| −0.3 | 3,000 / 10,000 / 30 | 134 | 93 | 11 | — / 3.7 s |
| −0.5 | 1,500 / 5,000 / 30 | 166 | 94 | 16 | 5.1 s / 8.6 s |
| −0.75 | 1,500 / 5,000 / 30 | 169 | 87 | 19 | 6.5 s / 11.1 s |
| −1.0 | 1,500 / 5,000 / 30 | 172 | 84 | 17 | 6.3 s / 11.1 s |
| **−0.5 (v3)** | **2,000 / 7,000 / 40** | **202 (57%)** | **104** | **19** | **6.1 s / 12.3 s** |
| −0.75 | 2,000 / 7,000 / 40 | 206 | 101 | 21 | 8.2 s / 11.9 s |

- With the reward, the node cap became the limit (at −0.3 and 600 nodes, 238 of 355 runs stopped on it); then the request budget. Both had to grow.
- Beyond −0.5 the gain is small and fewer found paths are hop-optimal, so −0.5 is the choice.
- **Tradeoff:** more found paths are longer than the true shortest (104 of 202 are optimal). Results are already scoped "among explored relationships", so this is honest, just less optimal.

## Effect on the demo pairs

- Deterministic runs now find Neymar–Snoop Dogg (they did not before) and Beyoncé–Alice Braga with strongest ≠ shortest. Diana–Churchill now finds the Cheam School family route; Coelho–Senna finds the 4-hop United Nations route. The three deterministic cases were re-recorded under v3.
- Lula–Snoop Dogg (6 hops, film credits only) is still not found deterministically, and the agent finds it only with DeepSeek reasoning on. It stays a recorded agent case.
- The agent is unaffected or better: Neymar–Snoop, Beyoncé–Alice Braga (strongest ≠ shortest) and Kanye–Gisele were all found in 28–37 s.
- The recorded agent cases keep the budgets they ran with (their traces record them).

## Cost

- Explored graphs reach ~1,500 nodes. The UI draws at most `ui.max_visible_nodes` (250) and hides leaves first. A Neymar–Snoop run sends ~830 KB of events, a 680 KB graph and 260 KB of Turtle over localhost.
- The deterministic run takes up to ~12 s (p90 6 s), against a 90 s wall time.

## Not done

- No change to `max_edges_per_expansion` (100) or `expansions_after_first_path` (4).
- The agent's prompt still lets it chase high-degree celebrities; that is what loses Lula–Snoop without reasoning.
- Reproduce: the harness was a scratch script (`run_deterministic` per variant with `model_copy` overrides); `benchmark_investigations.py` covers the random-pair column.
