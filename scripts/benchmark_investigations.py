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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.investigations.service import DEFAULT_GRAPH_PATH  # noqa: E402
from src.pairwork import benchmark_pair, default_workers, map_pairs  # noqa: E402


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
    ap.add_argument("--workers", type=int, default=default_workers(),
                    help="parallel processes (default: half the cores, at most 8; 1 = in-process)")
    args = ap.parse_args(argv)

    con = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    people = [f"Q{q}" for (q,) in con.execute(
        "SELECT qid FROM entity WHERE is_human AND label_en IS NOT NULL AND out_degree + in_degree BETWEEN ? AND ?",
        (args.min_degree, args.max_degree))]
    con.close()
    rng = random.Random(args.seed)
    candidates = [tuple(rng.sample(people, 2)) for _ in range(args.max_tries)]  # same draws as before
    rows, tries = [], 0
    print("source      target      full_hops  found_hops  stop                   expansions  exact_s  inv_s")
    results = map_pairs(args.db, benchmark_pair, candidates, args.workers)
    for r in results:  # input order: same rows whatever the worker count
        tries += 1
        if r is None:
            continue
        rows.append((r["full_hops"], r["found_hops"], r["inv_s"]))
        print(f"{r['source']:<11} {r['target']:<11} {r['full_hops']:>9}  "
              f"{r['found_hops'] if r['found_hops'] is not None else '-':>10}  "
              f"{r['stop']:<22} {r['expansions']:>10}  {r['exact_s']:>7.2f}  {r['inv_s']:>5.2f}", flush=True)
        if len(rows) >= args.pairs:
            results.close()  # cancels the pairs not started yet
            break
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
