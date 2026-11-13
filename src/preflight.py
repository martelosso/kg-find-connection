"""Workshop preflight: verify everything the demo needs, before the audience arrives.

    uv run python -m src.preflight            # offline checks only
    uv run python -m src.preflight --online   # also one tiny LLM call to prove the key works

Checks (✓ ok, ! warning, ✗ failure → non-zero exit):
- configuration files load and validate;
- every demo case's replay trace loads, rebuilds its graph, and the backend recomputes exactly the
  recorded paths; its endpoints and snapshot match the case; its fixture loads;
- the people graph (if installed) opens, has label search indexes and answers a search;
- workbench assets are vendored locally (no CDN), so the UI works without internet;
- an LLM key is configured (optional: replay and deterministic mode work without one);
- port 5000 is free.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ROOT, load_policy, load_search_config

Level = Literal["ok", "warn", "fail"]
ICON = {"ok": "✓", "warn": "!", "fail": "✗"}


@dataclass
class Check:
    level: Level
    name: str
    detail: str


def _checks_config(out: list[Check]) -> None:
    try:
        policy, search = load_policy(), load_search_config()
        by_kind = "".join(f", {kind}s {cap}" for kind, cap in
                          sorted(policy.pathfinding.max_intermediate_degree_by_kind.items()))
        out.append(Check("ok", "config", f"policy v{policy.version}, search v{search.version}, "
                                         f"hub cap {policy.pathfinding.max_intermediate_degree}{by_kind}"))
    except Exception as exc:  # noqa: BLE001 - report anything
        out.append(Check("fail", "config", f"{type(exc).__name__}: {exc}"))


def _checks_cases(out: list[Check], root: Path, cases_path: Path | None = None) -> None:
    from .graph.paths import compare_paths
    from .investigations.replay import load_trace, reconstruct
    from .investigations.service import load_demo_cases
    from .wikidata.offline import FixtureProvider

    try:
        cases = load_demo_cases(cases_path) if cases_path else load_demo_cases()
    except Exception as exc:  # noqa: BLE001
        out.append(Check("fail", "demo cases", f"config/demo_cases.yaml invalid: {exc}"))
        return
    with_trace = [c for c in cases.cases if c.trace]
    level: Level = "ok" if len(with_trace) >= 3 else "warn"
    out.append(Check(level, "demo cases", f"{len(with_trace)} replayable case(s) (the plan asks for at least 3)"))
    policy = load_policy()
    for case in with_trace:
        name = f"case {case.id}"
        try:
            trace = load_trace(root / case.trace)
            events = trace["events"]
            started = events[0].payload
            problems = []
            if (started["source_qid"], started["target_qid"]) != (case.source_qid, case.target_qid):
                problems.append("trace endpoints differ from the case")
            if case.verified_on and str(started.get("snapshot")) != str(case.verified_on):
                problems.append(f"trace snapshot {started.get('snapshot')} != verified_on {case.verified_on}")
            graph, cmp = reconstruct(events)
            if cmp is None or cmp.shortest.result is None:
                problems.append("trace has no path")
            else:
                recomputed = compare_paths(graph, case.source_qid, case.target_qid, policy, set(started["groups"]))
                if recomputed.shortest.result.path_id != cmp.shortest.result.path_id or \
                        recomputed.strongest.result.path_id != cmp.strongest.result.path_id:
                    problems.append("backend recomputes different paths (policy changed since recording?)")
            if case.fixture:
                FixtureProvider.from_files([root / case.fixture])
            if problems:
                out.append(Check("fail", name, "; ".join(problems)))
            else:
                s = cmp.shortest.result
                out.append(Check("ok", name, f"{case.title}: {len(events)} events, {len(graph.edges)} edges, "
                                             f"shortest {s.hop_count} hops, strongest {cmp.strongest.result.hop_count} hops"
                                             f"{' (same path)' if cmp.same_path else ''}, {started['strategy']}"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check("fail", name, f"{type(exc).__name__}: {exc}"))


def _checks_graph(out: list[Check], root: Path) -> None:
    from .investigations.service import DEFAULT_GRAPH_PATH

    path = Path(os.environ.get("PEOPLE_GRAPH_PATH") or DEFAULT_GRAPH_PATH)
    if not path.is_file():
        out.append(Check("warn", "people graph", f"{path} not found: live exploration is off, replays still work"))
        return
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
        indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        counts = meta.get("counts", {})
        snap = (meta.get("dump") or {}).get("dump_date")
        level: Level = "ok" if {"entity_label_en", "entity_label_es"} <= indexes else "warn"
        detail = (f"{path.name}: snapshot {snap}, {counts.get('humans', '?'):,} humans, "
                  f"{counts.get('edges', '?'):,} edges, {_size(path.stat().st_size)}")
        if level == "warn":
            detail += " (label search indexes missing: people search will be slow)"
        out.append(Check(level, "people graph", detail))
        db.close()
    except Exception as exc:  # noqa: BLE001
        out.append(Check("fail", "people graph", f"{type(exc).__name__}: {exc}"))


def _size(n: int) -> str:
    return f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"


def _checks_assets(out: list[Check], root: Path) -> None:
    ui = root / "src" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    external = [r for r in refs if not r.startswith("/")]
    missing = [r for r in refs if r.startswith("/") and not (ui / r.lstrip("/")).is_file()]
    if external or missing:
        out.append(Check("fail", "offline assets", f"external: {external} missing: {missing}"))
    else:
        out.append(Check("ok", "offline assets", f"{len(refs)} workbench assets served locally (no CDN)"))


def _checks_llm(out: list[Check], online: bool) -> None:
    from .agent.client import chat_client_from_env, llm_settings

    try:
        settings = llm_settings()
    except ValueError as exc:
        out.append(Check("fail", "LLM", str(exc)))
        return
    if settings is None:
        out.append(Check("warn", "LLM", "no API key: AI agent mode unavailable (deterministic + replay still work)"))
        return
    if settings.provider == "openrouter" and not settings.api_key.startswith("sk-or-"):
        out.append(Check("fail", "LLM", "OPENROUTER_API_KEY does not look like an OpenRouter key (they start with "
                                        "'sk-or-'). A DeepSeek key belongs in DEEPSEEK_API_KEY."))
        return
    if not online:
        out.append(Check("ok", "LLM", f"{settings.provider} / {settings.model} configured (not contacted; use --online)"))
        return
    try:
        from .agent.tools import tool_definitions

        reply = chat_client_from_env().complete(
            [{"role": "user", "content": "Reply with the single word: ready"}], tool_definitions())
        out.append(Check("ok", "LLM", f"{settings.provider} / {settings.model} answered: {(reply.content or '').strip()[:40]!r}"))
    except Exception as exc:  # noqa: BLE001
        out.append(Check("fail", "LLM", f"{settings.provider} call failed: {type(exc).__name__}: {str(exc)[:200]}"))


def _checks_port(out: list[Check], port: int = 5000) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    out.append(Check("warn" if busy else "ok", "port", f"{port} is {'already in use (server running?)' if busy else 'free'}"))


def run_checks(root: Path = ROOT, *, online: bool = False, port: bool = True,
               cases_path: Path | None = None) -> list[Check]:
    out: list[Check] = []
    _checks_config(out)
    _checks_cases(out, root, cases_path)
    _checks_graph(out, root)
    _checks_assets(out, root)
    _checks_llm(out, online)
    if port:
        _checks_port(out)
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # names, arrows: never crash on a cp1252 pipe (Windows)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--online", action="store_true", help="make one tiny LLM call to verify the key")
    args = ap.parse_args(argv)
    checks = run_checks(online=args.online)
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f" {ICON[c.level]} {c.name.ljust(width)}  {c.detail}")
    failed = [c for c in checks if c.level == "fail"]
    print(f"\n{'NOT READY' if failed else 'READY'}: {len(failed)} failure(s), "
          f"{sum(c.level == 'warn' for c in checks)} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
