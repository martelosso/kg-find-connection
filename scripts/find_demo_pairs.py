"""Find candidate demo pairs on the people graph. It SUGGESTS; a human picks and records them.

For each candidate pair of well-connected humans (or of the people in --seeds):
1. exact shortest/strongest on the complete graph (SqliteGraph, complete_configured_scope);
2. keep 2-4 hop connections whose path nodes all have English labels;
3. run the budgeted deterministic investigation and record whether it finds a path live;
4. rank by demo value: live-findable, strongest != shortest (a longer, stronger route),
   interpersonal hops (family / teacher-student), fewer anonymous nodes.

    uv run python scripts/find_demo_pairs.py --pairs 40
    uv run python scripts/find_demo_pairs.py --seeds seeds.txt     # one QID or exact name per line

Record the chosen ones with scripts/investigate.py SRC TGT --record <id> [--agent].
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_policy, load_search_config  # noqa: E402
from src.graph.model import is_qid  # noqa: E402
from src.graph.paths import compare_paths  # noqa: E402
from src.graph.store import SqliteGraph  # noqa: E402
from src.investigations.baseline import run_deterministic  # noqa: E402
from src.investigations.service import DEFAULT_GRAPH_PATH  # noqa: E402
from src.investigations.state import InvestigationState  # noqa: E402
from src.wikidata.offline import SqlitePeopleGraphProvider  # noqa: E402

INTERPERSONAL = {"family", "mentorship"}


def seeds_from_file(path: Path, provider: SqlitePeopleGraphProvider) -> list[str]:
    qids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if is_qid(line.upper()):
            qids.append(line.upper())
            continue
        exact = [m for m in provider.search_people(line, 5) if (m.label_en or "").casefold() == line.casefold()]
        if exact:
            qids.append(exact[0].qid)
        else:
            print(f"  seed not found exactly: {line!r}", file=sys.stderr)
    return list(dict.fromkeys(qids))


def well_connected(db: Path, limit: int, min_family: int) -> list[str]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        """SELECT e.qid FROM entity e
           WHERE e.is_human AND e.label_en IS NOT NULL AND e.label_es IS NOT NULL
             AND (SELECT COUNT(*) FROM edge x WHERE (x.subject = e.qid OR x.object = e.qid)
                  AND x.pid IN ('P26','P22','P25','P3373','P1066')) >= ?
           ORDER BY e.out_degree + e.in_degree DESC LIMIT ?""", (min_family, limit)).fetchall()
    return [f"Q{q}" for (q,) in rows]


def score(row: dict) -> float:
    s = 0.0
    s += 3 if row["live_found"] else 0
    s += 2 if row["divergent"] else 0
    s += 1.0 * row["interpersonal_hops"]
    s -= 1.5 * row["unlabeled_nodes"]
    s -= 0.3 * max(0, row["shortest_hops"] - 3)
    return round(s, 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_GRAPH_PATH)
    ap.add_argument("--seeds", type=Path, help="file with QIDs or exact English names")
    ap.add_argument("--pool", type=int, default=400, help="well-connected people to sample from")
    ap.add_argument("--min-family", type=int, default=2, help="min family/mentorship links per candidate")
    ap.add_argument("--pairs", type=int, default=40, help="pairs to evaluate")
    ap.add_argument("--max-hops", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out", type=Path, default=None, help="write all evaluated rows as JSON")
    args = ap.parse_args(argv)

    policy, search = load_policy(), load_search_config()
    graph, provider = SqliteGraph(args.db), SqlitePeopleGraphProvider(args.db)
    if args.seeds:
        people = seeds_from_file(args.seeds, provider)
        pairs = list(itertools.combinations(people, 2))
    else:
        people = well_connected(args.db, args.pool, args.min_family)
        rng = random.Random(args.seed)
        pairs = [tuple(rng.sample(people, 2)) for _ in range(args.pairs * 3)]
    print(f"{len(people)} candidate people, evaluating up to {args.pairs if not args.seeds else len(pairs)} pairs",
          file=sys.stderr)

    rows = []
    for s, t in pairs:
        if len(rows) >= (len(pairs) if args.seeds else args.pairs):
            break
        t0 = time.time()
        exact = compare_paths(graph, s, t, policy)
        exact_s = time.time() - t0
        short, strong = exact.shortest.result, exact.strongest.result
        if short is None or short.hop_count < 2 or short.hop_count > args.max_hops:
            continue
        nodes = {n.qid: n for r in (short, strong) for n in r.nodes}
        state = InvestigationState(s, t, policy.default_groups, search.budgets, snapshot=provider.snapshot)
        t0 = time.time()
        run_deterministic(state, provider, policy, search)
        live = state.comparison.shortest.result if state.comparison else None
        row = {
            "source": s, "target": t,
            "source_label": graph.node(s).label(), "target_label": graph.node(t).label(),
            "shortest_hops": short.hop_count, "strongest_hops": strong.hop_count,
            "shortest_cost": round(short.total_cost, 3), "strongest_cost": round(strong.total_cost, 3),
            "divergent": not exact.same_path,
            "interpersonal_hops": sum(h.relation_group in INTERPERSONAL for h in strong.hops),
            "unlabeled_nodes": sum(1 for n in nodes.values() if not n.label_en),
            "strongest_route": " → ".join(n.label_en or n.qid for n in strong.nodes),
            "live_found": live is not None, "live_hops": live.hop_count if live else None,
            "live_requests": state.provider_requests, "live_stop": state.stop_reason,
            "exact_seconds": round(exact_s, 2), "live_seconds": round(time.time() - t0, 2),
        }
        row["score"] = score(row)
        rows.append(row)
        print(f"  {row['score']:>5}  {row['source_label']} ↔ {row['target_label']}: {row['shortest_hops']}/"
              f"{row['strongest_hops']} hops, live {'yes' if row['live_found'] else 'no'}", file=sys.stderr, flush=True)

    rows.sort(key=lambda r: -r["score"])
    print("\nTop candidates (score · pair · shortest/strongest hops · live · strongest route):")
    for r in rows[:15]:
        print(f"{r['score']:>6}  {r['source']} {r['source_label']}  ↔  {r['target']} {r['target_label']}  "
              f"{r['shortest_hops']}/{r['strongest_hops']}{' DIVERGENT' if r['divergent'] else ''}  "
              f"live={'yes' if r['live_found'] else 'no'}\n        {r['strongest_route']}")
    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
