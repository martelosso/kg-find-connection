"""Run a deterministic investigation offline and print its timeline; optionally record a demo case.

    uv run python scripts/investigate.py Q7186 Q37463                     # print only
    uv run python scripts/investigate.py --search "Marie Curie"           # find QIDs
    uv run python scripts/investigate.py Q7186 Q37463 --record curie-family --title "..."

``--record`` writes data/fixtures/cases/<id>.json (the neighbourhoods the run used) and
data/fixtures/event_traces/<id>.json (the run's events), then RE-RUNS the investigation against
the fixture alone and fails unless it reproduces the identical event sequence. It prints the
demo_cases.yaml entry to add; it never edits that file and never invents a pair.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_policy, load_search_config  # noqa: E402
from src.investigations.baseline import run_deterministic  # noqa: E402
from src.investigations.replay import normalized_events, trace_dict  # noqa: E402
from src.investigations.service import CASES_DIR, DEFAULT_GRAPH_PATH, TRACES_DIR  # noqa: E402
from src.investigations.state import InvestigationState  # noqa: E402
from src.wikidata.offline import FixtureProvider, SqlitePeopleGraphProvider, build_case_fixture  # noqa: E402


def describe(state: InvestigationState) -> None:
    for e in state.events.events:
        p = e.payload
        if e.type == "agent.decision":
            print(f"  agent: {p['summary']}")
        elif e.type == "tool.started" and "qid" in p:
            print(f"  [{p['step']:>2}] expand {p['label']} ({p['qid']}, {p['side']} side) — {p.get('reason', '')}")
        elif e.type == "tool.started":
            print(f"  [{p['step']:>2}] {p['tool']} {p['arguments']}")
        elif e.type == "graph.expanded":
            flag = "" if p["complete"] else "  (truncated)"
            print(f"       +{len(p['added_nodes'])} nodes, +{len(p['added_edges'])} edges{flag}")
        elif e.type == "tool.completed" and not p["ok"]:
            print(f"       ERROR {p['error']['code']}: {p['error']['message']}")
        elif e.type == "paths.updated":
            print("       paths updated")
        elif e.type.startswith("investigation.") and e.type != "investigation.started":
            print(f"{e.type}: {p.get('outcome')} (stop: {p.get('stop_reason')}) {p.get('stats', '')}")
            if p.get("error"):
                print(f"  error: {p['error']}")
    cmp = state.comparison
    if cmp:
        for out in (cmp.shortest, cmp.strongest):
            r = out.result
            if r is None:
                print(f"{out.mode}: {out.status}")
                continue
            labels = {n.qid: (n.label_en or n.label_es or n.qid) for n in r.nodes}
            hops = " ".join(f"-[{h.label}]-> {labels[h.to_qid]}" for h in r.hops)
            print(f"{out.mode}: {r.hop_count} hops, cost {r.total_cost:.3f}: {labels[r.source_qid]} {hops}")
        print(f"same path: {cmp.same_path}; scope: {cmp.scope}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?")
    ap.add_argument("target", nargs="?")
    ap.add_argument("--db", type=Path, default=DEFAULT_GRAPH_PATH)
    ap.add_argument("--search")
    ap.add_argument("--groups", help="comma-separated relation groups (default: policy defaults)")
    ap.add_argument("--agent", action="store_true", help="use the LLM agent (needs OPENROUTER_API_KEY)")
    ap.add_argument("--record", metavar="CASE_ID")
    ap.add_argument("--title", default="")
    ap.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    ap.add_argument("--traces-dir", type=Path, default=TRACES_DIR)
    args = ap.parse_args(argv)

    provider = SqlitePeopleGraphProvider(args.db)
    if args.search:
        for m in provider.search_people(args.search, 10):
            print(f"{m.qid:>12}  degree {m.degree:>4}  {m.label_en} / {m.label_es}")
        return 0
    if not (args.source and args.target):
        ap.error("source and target QIDs are required")

    policy, search = load_policy(), load_search_config()
    groups = frozenset(args.groups.split(",")) if args.groups else policy.default_groups

    def run(p):
        state = InvestigationState(args.source, args.target, groups, search.budgets, snapshot=p.snapshot)
        return run_deterministic(state, p, policy, search)

    if args.agent:
        from src.agent.client import chat_client_from_env
        from src.agent.investigator import run_agent

        chat = chat_client_from_env()
        if chat is None:
            print("No LLM API key (OPENROUTER_API_KEY or DEEPSEEK_API_KEY) is set: the agent is unavailable (run without --agent for the "
                  "deterministic exploration mode).", file=sys.stderr)
            return 3
        state = InvestigationState(args.source, args.target, groups, search.budgets, strategy="agent",
                                   snapshot=provider.snapshot)
        run_agent(state, provider, policy, search, chat)
        describe(state)
        answer = state.events.events[-1].payload.get("answer") or {}
        print(f"answer ({answer.get('summary_source')}): {answer.get('summary')}")
        if args.record:
            print("--record is only supported for deterministic runs", file=sys.stderr)
        return 0

    state = run(provider)
    describe(state)
    if not args.record:
        return 0
    if state.status == "failed":
        print("not recording a failed investigation", file=sys.stderr)
        return 1

    expanded = [r.qid for r in state.expansions if r.error is None]
    cap = max(500, search.budgets.max_edges_per_expansion)
    fixture = build_case_fixture(provider, args.record, expanded + [args.source, args.target],
                                 policy.properties.keys(), cap=cap)
    replayed = run(FixtureProvider([fixture]))
    if normalized_events(replayed.events.events) != normalized_events(state.events.events):
        print("fixture does NOT reproduce the run; not writing files", file=sys.stderr)
        return 2

    args.cases_dir.mkdir(parents=True, exist_ok=True)
    args.traces_dir.mkdir(parents=True, exist_ok=True)
    fx_path = args.cases_dir / f"{args.record}.json"
    tr_path = args.traces_dir / f"{args.record}.json"
    fx_path.write_text(json.dumps(fixture, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    trace = trace_dict(state, args.record, {"provider": provider.name, "snapshot": provider.snapshot,
                                            "policy_version": policy.version, "search_version": search.version})
    tr_path.write_text(json.dumps(trace, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\nwrote {fx_path} and {tr_path} (fixture reproduces the run)")
    s, t = state.graph.node(args.source), state.graph.node(args.target)
    print("\nAdd to config/demo_cases.yaml after checking the paths above:\n")
    print(f"  - id: {args.record}\n    title: {json.dumps(args.title or f'{s.label()} ↔ {t.label()}', ensure_ascii=False)}"
          f"\n    source_qid: {args.source}\n    source_label: {json.dumps(s.label(), ensure_ascii=False)}"
          f"\n    target_qid: {args.target}\n    target_label: {json.dumps(t.label(), ensure_ascii=False)}"
          f"\n    fixture: data/fixtures/cases/{args.record}.json"
          f"\n    trace: data/fixtures/event_traces/{args.record}.json"
          f"\n    verified_on: \"{provider.snapshot}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
