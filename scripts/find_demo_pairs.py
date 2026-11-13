"""Find candidate demo pairs on the people graph. It SUGGESTS; a human picks and records them.

For each candidate pair of well-connected humans (or of the people in --seeds):
1. exact shortest/strongest on the complete graph (in-memory indexed people graph, complete_configured_scope);
2. keep 2-4 hop connections whose path nodes all have English labels;
3. run the budgeted deterministic investigation and record whether it finds a path live;
4. rank by demo value: live-findable, strongest != shortest (a longer, stronger route),
   interpersonal hops (family / teacher-student), fewer anonymous nodes.

    uv run python scripts/find_demo_pairs.py --pairs 40
    uv run python scripts/find_demo_pairs.py --seeds config/demo_seeds.txt --resolve-only   # check names
    uv run python scripts/find_demo_pairs.py --seeds config/demo_seeds.txt --out pairs.json  # every seed pair
    uv run python scripts/find_demo_pairs.py --seeds config/demo_seeds.txt --cross recent historical --pairs 400

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
from src.graph.store import open_people_graph  # noqa: E402
from src.investigations.baseline import run_deterministic  # noqa: E402
from src.investigations.service import DEFAULT_GRAPH_PATH  # noqa: E402
from src.investigations.state import InvestigationState  # noqa: E402
from src.wikidata.offline import SqlitePeopleGraphProvider  # noqa: E402

INTERPERSONAL = {"family", "mentorship"}
LOW_DEGREE = 5  # a famous person usually has more links; fewer suggests a namesake


def read_seed_lines(path: Path) -> list[tuple[str, str]]:
    """(entry, group) pairs: one entry per line, '#' starts a comment, blank lines ignored.

    A line ``[name]`` starts a group (default group: ``default``); ``--cross A B`` pairs only
    people of group A with people of group B, e.g. today's celebrities with historical figures.
    """
    out, group = [], "default"
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = " ".join(line.split("#", 1)[0].split())
        if entry.startswith("[") and entry.endswith("]"):
            group = entry[1:-1].strip() or "default"
        elif entry:
            out.append((entry, group))
    return out


def seed_pairs(resolved: list[dict], cross: tuple[str, str] | None) -> list[tuple[str, str]]:
    """All distinct pairs of resolved seeds; with ``cross``, only pairs across the two groups."""
    found = [r for r in resolved if r["qid"]]
    if cross is None:
        people = list(dict.fromkeys(r["qid"] for r in found))
        return list(itertools.combinations(people, 2))
    a = list(dict.fromkeys(r["qid"] for r in found if r["group"] == cross[0]))
    b = list(dict.fromkeys(r["qid"] for r in found if r["group"] == cross[1]))
    return list(dict.fromkeys((s, t) for s in a for t in b if s != t))


def resolve_seed(con: sqlite3.Connection, entry: str) -> dict:
    """Resolve one seed to a human QID by QID or EXACT label (en, then es; case-insensitive).

    Exact lookup, not the UI's prefix search: a prefix search ranked by degree can crowd out the
    exact name ("John Adams" behind "John Adams Dix"). Several humans with the same label are
    reported as ambiguous; the best-connected (then lowest QID) is taken.
    """
    if is_qid(entry.upper()):
        rows = con.execute("SELECT qid, label_en, label_es, out_degree + in_degree FROM entity "
                           "WHERE qid = ? AND is_human", (int(entry[1:]),)).fetchall()
        via = "qid"
    else:
        rows, via = [], None
        for lang in ("en", "es"):
            rows = con.execute(
                f"SELECT qid, label_en, label_es, out_degree + in_degree AS deg FROM entity "
                f"WHERE is_human AND label_{lang} = ? COLLATE NOCASE ORDER BY deg DESC, qid", (entry,)).fetchall()
            if rows:
                via = lang
                break
    if not rows:
        return {"entry": entry, "qid": None, "label": None, "degree": 0, "matches": 0, "via": None}
    q, l_en, l_es, deg = rows[0]
    return {"entry": entry, "qid": f"Q{q}", "label": l_en or l_es or f"Q{q}", "degree": deg,
            "matches": len(rows), "via": via, "others": [f"Q{r[0]}" for r in rows[1:6]]}


def resolve_seeds(db: Path, entries: list[tuple[str, str]]) -> list[dict]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return [{**resolve_seed(con, e), "group": g} for e, g in entries]
    finally:
        con.close()


def print_resolution(resolved: list[dict]) -> None:
    for r in resolved:
        if r["qid"] is None:
            print(f"  ✗ [{r.get('group', 'default')}] {r['entry']}: not found (no human with this exact en/es label)", file=sys.stderr)
            continue
        notes = []
        if r["matches"] > 1:
            notes.append(f"AMBIGUOUS: {r['matches']} humans, others {', '.join(r['others'])}")
        if r["via"] != "qid" and r["degree"] < LOW_DEGREE:
            notes.append("low degree: is this the famous one? pin a QID if not")
        note = ("  " + "; ".join(notes)) if notes else ""
        print(f"  {'!' if note else '✓'} [{r.get('group', 'default')}] {r['entry']} → {r['qid']} {r['label']} (degree {r['degree']}){note}",
              file=sys.stderr)


def well_connected(db: Path, limit: int, min_family: int) -> list[str]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        """SELECT e.qid FROM entity e
           WHERE e.is_human AND e.label_en IS NOT NULL AND e.label_es IS NOT NULL
             AND (SELECT COUNT(*) FROM edge x WHERE (x.subject = e.qid OR x.object = e.qid)
                  AND x.pid IN ('P26','P22','P25','P3373','P40','P451','P1038','P1066')) >= ?
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
    for stream in (sys.stdout, sys.stderr):  # names, arrows: never crash on a cp1252 pipe (Windows)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_GRAPH_PATH)
    ap.add_argument("--seeds", type=Path, help="file with QIDs or exact English names")
    ap.add_argument("--pool", type=int, default=400, help="well-connected people to sample from")
    ap.add_argument("--min-family", type=int, default=2, help="min family/mentorship links per candidate")
    ap.add_argument("--pairs", type=int, default=None,
                    help="pairs to keep (default 40; with --seeds: every seed pair, or a random sample of this many)")
    ap.add_argument("--max-hops", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--cross", nargs=2, metavar=("GROUP_A", "GROUP_B"),
                    help="with --seeds: only pair people of seed group A with people of group B")
    ap.add_argument("--resolve-only", action="store_true", help="with --seeds: print the name → QID resolution and stop")
    ap.add_argument("--out", type=Path, default=None, help="write all evaluated rows as JSON")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    if args.seeds:
        resolved = resolve_seeds(args.db, read_seed_lines(args.seeds))
        print_resolution(resolved)
        if args.resolve_only:
            return 0 if all(r["qid"] for r in resolved) else 1
        if args.cross:
            groups = {r["group"] for r in resolved}
            missing = [g for g in args.cross if g not in groups]
            if missing:
                ap.error(f"--cross: no seed group {missing} in {args.seeds} (groups: {sorted(groups)})")
        people = list(dict.fromkeys(r["qid"] for r in resolved if r["qid"]))
        pairs = seed_pairs(resolved, tuple(args.cross) if args.cross else None)
        if args.pairs is not None and args.pairs < len(pairs):
            pairs = rng.sample(pairs, args.pairs)
        wanted = len(pairs)
    elif args.resolve_only or args.cross:
        ap.error("--resolve-only and --cross need --seeds")
    else:
        people = well_connected(args.db, args.pool, args.min_family)
        wanted = args.pairs or 40
        pairs = [tuple(rng.sample(people, 2)) for _ in range(wanted * 3)]

    policy, search = load_policy(), load_search_config()
    graph, provider = open_people_graph(args.db), SqlitePeopleGraphProvider(args.db)
    print(f"{len(people)} candidate people, {len(pairs)} pairs to try, keeping up to {wanted} "
          f"with a 2-{args.max_hops} hop connection", file=sys.stderr)

    rows = []
    for i, (s, t) in enumerate(pairs, 1):
        if len(rows) >= wanted:
            break
        if i % 25 == 0:
            print(f"  … {i}/{len(pairs)} pairs tried, {len(rows)} kept", file=sys.stderr, flush=True)
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
