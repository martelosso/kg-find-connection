"""Measure how often the bounded deterministic investigation finds what the complete graph has.

Samples random human pairs that ARE connected within the hop limit in the full people graph
(exact search on the indexed people graph), runs the budgeted investigation for each, and reports recall,
hop optimality and timing. Use it to tune config/search.yaml and the hub cap.

    uv run python scripts/benchmark_investigations.py --db data/wikidata/people_graph.sqlite --pairs 30
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_policy, load_search_config  # noqa: E402
from src.graph.paths import compute_shortest_path  # noqa: E402
from src.graph.store import open_people_graph  # noqa: E402
from src.investigations.baseline import run_deterministic  # noqa: E402
from src.investigations.service import DEFAULT_GRAPH_PATH  # noqa: E402
from src.investigations.state import InvestigationState  # noqa: E402
from src.wikidata.offline import SqlitePeopleGraphProvider  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # names, arrows: never crash on a cp1252 pipe (Windows)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_GRAPH_PATH)
    ap.add_argument("--pairs", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--min-degree", type=int, default=4)
    ap.add_argument("--max-degree", type=int, default=200)
    ap.add_argument("--max-tries", type=int, default=1000)
    args = ap.parse_args(argv)

    pol, search = load_policy(), load_search_config()
    graph, provider = open_people_graph(args.db), SqlitePeopleGraphProvider(args.db)
    con = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    people = [f"Q{q}" for (q,) in con.execute(
        "SELECT qid FROM entity WHERE is_human AND label_en IS NOT NULL AND out_degree + in_degree BETWEEN ? AND ?",
        (args.min_degree, args.max_degree))]
    rng = random.Random(args.seed)
    rows, tries = [], 0
    print("source      target      full_hops  found_hops  stop                   expansions  exact_s  inv_s")
    while len(rows) < args.pairs and tries < args.max_tries:
        tries += 1
        s, t = rng.sample(people, 2)
        t0 = time.time()
        exact = compute_shortest_path(graph, s, t, pol)
        exact_s = time.time() - t0
        if exact.result is None:
            continue
        state = InvestigationState(s, t, pol.default_groups, search.budgets, snapshot=provider.snapshot)
        t0 = time.time()
        run_deterministic(state, provider, pol, search)
        inv_s = time.time() - t0
        found = state.comparison.shortest.result if state.comparison else None
        rows.append((exact.result.hop_count, found.hop_count if found else None, inv_s))
        print(f"{s:<11} {t:<11} {exact.result.hop_count:>9}  {found.hop_count if found else '-':>10}  "
              f"{state.stop_reason:<22} {len(state.expansions):>10}  {exact_s:>7.2f}  {inv_s:>5.2f}", flush=True)
    if not rows:
        print("no connected pairs found")
        return 1
    found = [r for r in rows if r[1] is not None]
    print(f"\npairs {len(rows)} (from {tries} tries)  found {len(found)}  "
          f"hop-optimal {sum(1 for r in found if r[1] == r[0])}  "
          f"median investigation {statistics.median(r[2] for r in rows):.2f}s  max {max(r[2] for r in rows):.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
