"""Measure hubs in the people graph to choose the hub cap (pathfinding.max_intermediate_degree).

A non-person node whose degree exceeds the cap may be a path endpoint but never an intermediate
node. Too high a cap and most paths read "both studied at Harvard"; too low and pairs that do
connect through a real institution look unconnected. This script MEASURES; a human picks the cap
and writes it into config/relationships.yaml (bump the policy version).

    uv run python scripts/analyze_hubs.py                                  # distribution + sweep
    uv run python scripts/analyze_hubs.py --caps 1000 2000 5000 none --pairs 150
    uv run python scripts/analyze_hubs.py --seeds config/demo_seeds.txt --cross recent historical
    uv run python scripts/analyze_hubs.py --no-sweep --top 40              # distribution only

Part 1, distribution: degree percentiles of non-person nodes (organisations, positions, other),
how many exceed each candidate cap, and the top hubs with their per-property breakdown.

Part 2, cap sweep: for each cap, the EXACT shortest and strongest paths for the same sample of
pairs (random people with at least --min-degree links, or the seeds), and per cap:
connectivity, hops, how many paths pass through an organisation and through interpersonal links,
the busiest intermediate hubs, and run time. "none" means no cap (can be slow on the full graph).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.config import RelationshipPolicy, load_policy  # noqa: E402
from src.graph.store import IndexedPeopleGraph  # noqa: E402
from src.investigations.service import DEFAULT_GRAPH_PATH  # noqa: E402
from src.pairwork import Context, default_workers, hub_sweep_pair, map_pairs, with_cap  # noqa: E402,F401

THRESHOLDS = (100, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000)
PERCENTILES = (50, 90, 99, 99.9, 99.99)
NON_PERSON = ("organization", "work", "position", "place", "other")


def parse_cap(text: str) -> int | None:
    return None if text.lower() in ("none", "off", "no") else int(text)


def fmt_cap(cap: int | None) -> str:
    return "none" if cap is None else f"{cap:,}"


# ---- part 1: distribution ----------------------------------------------------------------------


def distribution(graph: IndexedPeopleGraph, top: int) -> dict:
    import numpy as np

    out: dict = {"kinds": {}}
    for kind in NON_PERSON:
        _, deg = graph.degrees_of_kind((kind,))
        if len(deg) == 0:
            out["kinds"][kind] = {"count": 0}
            continue
        out["kinds"][kind] = {
            "count": int(len(deg)),
            "percentiles": {str(p): float(np.percentile(deg, p)) for p in PERCENTILES},
            "max": int(deg.max()),
            "above": {str(t): int((deg > t).sum()) for t in THRESHOLDS},
        }
    qids, deg = graph.degrees_of_kind(NON_PERSON)
    order = np.argsort(-deg, kind="stable")[:top]
    hubs = []
    for i in order:
        qid = f"Q{int(qids[i])}"
        node = graph.node(qid)
        by_pid = Counter(e.pid for e in graph.incident(qid, frozenset(graph_pids(graph))))
        hubs.append({"qid": qid, "label": node.label() if node else qid, "kind": node.kind if node else "?",
                     "degree": int(deg[i]), "by_pid": dict(by_pid.most_common())})
    out["top_hubs"] = hubs
    return out


def graph_pids(graph: IndexedPeopleGraph) -> list[str]:
    return list(graph._pid_names)  # noqa: SLF001 - every PID present in the graph


def print_distribution(d: dict) -> None:
    print("\n== Non-person node degrees (degree = all extracted links, as the hub rule uses) ==")
    head = "kind           count      " + "  ".join(f"p{p:<7}" for p in PERCENTILES) + "  max"
    print(head)
    for kind, s in d["kinds"].items():
        if not s["count"]:
            print(f"{kind:<14} 0")
            continue
        pct = "  ".join(f"{s['percentiles'][str(p)]:<8,.0f}" for p in PERCENTILES)
        print(f"{kind:<14} {s['count']:<10,} {pct}  {s['max']:,}")
    print("\nNodes ABOVE each candidate cap (these could not be intermediate path nodes):")
    print("kind           " + "  ".join(f">{t:<7,}" for t in THRESHOLDS))
    for kind, s in d["kinds"].items():
        if s["count"]:
            print(f"{kind:<14} " + "  ".join(f"{s['above'][str(t)]:<8,}" for t in THRESHOLDS))
    print(f"\nTop {len(d['top_hubs'])} hubs:")
    for h in d["top_hubs"]:
        pids = ", ".join(f"{p} {n:,}" for p, n in h["by_pid"].items())
        print(f"  {h['degree']:>9,}  {h['qid']:<11} {h['label'][:48]:<48} {h['kind']:<12} {pids}")


# ---- part 2: sweep -----------------------------------------------------------------------------


def sample_pairs(graph: IndexedPeopleGraph, n: int, min_degree: int, seed: int) -> list[tuple[str, str]]:
    people = graph.qids_of_kind("person", min_degree=min_degree)
    if len(people) < 2:
        return []
    rng = random.Random(seed)
    pairs = []
    while len(pairs) < n:
        a, b = rng.sample(range(len(people)), 2)
        pairs.append((f"Q{int(people[a])}", f"Q{int(people[b])}"))
    return pairs


def seed_pairs(db: Path, seeds: Path, cross: list[str] | None, n: int, seed: int) -> list[tuple[str, str]]:
    import find_demo_pairs as fdp

    resolved = fdp.resolve_seeds(db, fdp.read_seed_lines(seeds))
    pairs = fdp.seed_pairs(resolved, tuple(cross) if cross else None)
    if n < len(pairs):
        pairs = random.Random(seed).sample(pairs, n)
    return pairs


def sweep(graph: IndexedPeopleGraph, policy: RelationshipPolicy, caps: list[int | None],
          pairs: list[tuple[str, str]], groups: frozenset[str] | None, hub_floor: int,
          *, workers: int = 1) -> list[dict]:
    """Per-pair exact searches (in parallel when workers > 1), aggregated per cap in pair order."""
    ctx = Context(graph._path, graph=graph)  # noqa: SLF001 - in-process path reuses the open graph
    ctx.policy = policy
    per_pair = list(map_pairs(graph._path, hub_sweep_pair, pairs, workers, context=ctx,  # noqa: SLF001
                              caps=caps, groups=groups, hub_floor=hub_floor))
    rows = []
    for i, cap in enumerate(caps):
        recs = [p[i] for p in per_pair]
        ok = [r for r in recs if r["connected"]]
        strong = [r for r in ok if "strong_hops" in r]
        org_degs = [r["max_org_degree"] for r in ok if r["max_org_degree"] is not None]
        times = [r["seconds"] for r in recs]
        hub_use: Counter = Counter()
        for r in ok:
            hub_use.update(r["hubs"])
        rows.append({
            "cap": cap, "pairs": len(pairs), "connected": len(ok), "truncated": sum(r["truncated"] for r in recs),
            "median_short_hops": statistics.median(r["short_hops"] for r in ok) if ok else None,
            "median_strong_hops": statistics.median(r["strong_hops"] for r in strong) if strong else None,
            "short_via_org": sum(r["short_via_org"] for r in ok),
            "median_max_org_degree": statistics.median(org_degs) if org_degs else None,
            "divergent": sum(r["divergent"] for r in strong),
            "strong_has_interpersonal": sum(r["strong_has_interpersonal"] for r in strong),
            "strong_interpersonal_only": sum(r["strong_interpersonal_only"] for r in strong),
            "median_s": round(statistics.median(times), 4) if times else None,
            "max_s": round(max(times), 3) if times else None,
            "busiest_hubs": [{"qid": q, "label": (graph.node(q).label() if graph.node(q) else q), "paths": c,
                              "degree": graph.node_profile(q)[1]} for q, c in hub_use.most_common(5)],
        })
    return rows


def num(x) -> str:
    return "-" if x is None else f"{x:g}"


def pct(x: int, n: int) -> str:
    return f"{100 * x / n:.0f}%" if n else "-"


def print_sweep(rows: list[dict], hub_floor: int) -> None:
    print("\n== Cap sweep: exact shortest/strongest on the same pairs ==")
    print("cap        connected  short hops  strong hops  short via org  med max org deg  "
          "strong≠short  strong w/ family|mentor  only family|mentor  trunc  median s  max s")
    for r in rows:
        c = r["connected"]
        print(f"{fmt_cap(r['cap']):<10} {pct(c, r['pairs']):>9}  {num(r['median_short_hops']):>10}  "
              f"{num(r['median_strong_hops']):>11}  {pct(r['short_via_org'], c):>13}  "
              f"{num(r['median_max_org_degree']):>15}  {pct(r['divergent'], c):>12}  "
              f"{pct(r['strong_has_interpersonal'], c):>23}  {pct(r['strong_interpersonal_only'], c):>18}  "
              f"{r['truncated']:>5}  {num(r['median_s']):>8}  {num(r['max_s']):>5}")
    print(f"\nBusiest intermediate hubs on shortest paths (degree ≥ {hub_floor:,}):")
    for r in rows:
        hubs = "; ".join(f"{h['label'][:30]} ({h['degree']:,}) ×{h['paths']}" for h in r["busiest_hubs"]) or "-"
        print(f"  cap {fmt_cap(r['cap']):<8} {hubs}")
    print("\nPercentages of connected pairs, except 'connected' (of all pairs). A good cap keeps most "
          "connectivity while fewer shortest paths lean on giant hubs.")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # names, arrows: never crash on a cp1252 pipe (Windows)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_GRAPH_PATH)
    ap.add_argument("--top", type=int, default=25, help="top hubs to list")
    ap.add_argument("--caps", nargs="+", default=["500", "1000", "2000", "5000", "10000"],
                    help="candidate caps; 'none' = no cap")
    ap.add_argument("--pairs", type=int, default=100)
    ap.add_argument("--min-degree", type=int, default=3, help="random pairs: people with at least this many links")
    ap.add_argument("--seeds", type=Path, help="use seed pairs (config/demo_seeds.txt format) instead of random")
    ap.add_argument("--cross", nargs=2, metavar=("GROUP_A", "GROUP_B"))
    ap.add_argument("--groups", help="comma-separated relation groups (default: policy defaults)")
    ap.add_argument("--hub-floor", type=int, default=1000, help="degree from which a hub is listed as 'busiest'")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--no-sweep", action="store_true")
    ap.add_argument("--workers", type=int, default=default_workers(),
                    help="parallel processes for the sweep (default: half the cores, at most 8; 1 = in-process)")
    ap.add_argument("--out", type=Path, help="write all numbers as JSON")
    args = ap.parse_args(argv)

    t0 = time.time()
    graph = IndexedPeopleGraph(args.db)
    policy = load_policy()
    print(f"{args.db}: snapshot {graph.snapshot}, policy v{policy.version}, current cap "
          f"{fmt_cap(policy.pathfinding.max_intermediate_degree)}, opened in {time.time() - t0:.1f}s")
    report: dict = {"db": str(args.db), "snapshot": graph.snapshot, "policy_version": policy.version,
                    "current_cap": policy.pathfinding.max_intermediate_degree}
    report["distribution"] = distribution(graph, args.top)
    print_distribution(report["distribution"])
    if not args.no_sweep:
        if args.seeds:
            pairs = seed_pairs(args.db, args.seeds, args.cross, args.pairs, args.seed)
        else:
            pairs = sample_pairs(graph, args.pairs, args.min_degree, args.seed)
        caps = [parse_cap(c) for c in args.caps]
        groups = frozenset(args.groups.split(",")) if args.groups else None
        print(f"\nsweeping {len(caps)} caps over {len(pairs)} pairs ({args.workers} worker(s)) ...", flush=True)
        report["sweep"] = sweep(graph, policy, caps, pairs, groups, args.hub_floor, workers=args.workers)
        report["workers"] = args.workers
        report["pairs"] = pairs
        print_sweep(report["sweep"], args.hub_floor)
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")
    graph.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
