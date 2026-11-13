"""scripts/analyze_hubs.py on a synthetic graph with one big hub and a family route around it."""

import importlib.util
import json

import pytest

from src.graph.store import IndexedPeopleGraph

from .graph_helpers import build_people_db

# 12 people studied at Big U (degree 12). A and B also connect through family (3 hops).
PEOPLE = {f"Q9000{i:02d}": f"P{i}" for i in range(12)}
ORGS = {"Q910001": "Big U", "Q910002": "Small Club"}
A, B = "Q900000", "Q900001"
TRIPLES = ([(q, "P69", "Q910001") for q in PEOPLE]
           + [(A, "P26", "Q900002"), ("Q900002", "P3373", "Q900003"), ("Q900003", "P40", B),
              ("Q900004", "P54", "Q910002"), ("Q900005", "P54", "Q910002")])


@pytest.fixture(scope="module")
def mod(request):
    root = request.config.rootpath
    spec = importlib.util.spec_from_file_location("analyze_hubs", root / "scripts" / "analyze_hubs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def db(request, tmp_path_factory):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("hubs"), PEOPLE, ORGS, TRIPLES)


def test_distribution_counts_and_top_hubs(mod, db):
    g = IndexedPeopleGraph(db)
    try:
        d = mod.distribution(g, top=2)
    finally:
        g.close()
    org = d["kinds"]["organization"]
    assert org["count"] == 2 and org["max"] == 12 and org["above"]["100"] == 0
    assert [h["qid"] for h in d["top_hubs"]] == ["Q910001", "Q910002"]
    assert d["top_hubs"][0]["by_pid"] == {"P69": 12} and d["top_hubs"][1]["by_pid"] == {"P54": 2}


def test_sweep_shows_the_cap_trading_hub_paths_for_family_paths(mod, db):
    from src.config import load_policy

    g = IndexedPeopleGraph(db)
    try:
        rows = mod.sweep(g, load_policy(), [5, None], [(A, B), ("Q900006", "Q900007")], None, hub_floor=10)
    finally:
        g.close()
    capped, uncapped = rows
    # Without a cap both pairs connect in 2 hops through Big U; with cap 5 Big U is blocked:
    # A-B still connect through family (3 hops), the other pair is cut off.
    assert uncapped["connected"] == 2 and uncapped["median_short_hops"] == 2 and uncapped["short_via_org"] == 2
    assert uncapped["busiest_hubs"][0]["qid"] == "Q910001" and uncapped["busiest_hubs"][0]["paths"] == 2
    assert capped["connected"] == 1 and capped["median_short_hops"] == 3 and capped["short_via_org"] == 0
    assert capped["strong_interpersonal_only"] == 1 and capped["busiest_hubs"] == []


def test_main_writes_report(mod, db, tmp_path, capsys):
    out = tmp_path / "hubs.json"
    assert mod.main(["--db", str(db), "--caps", "5", "none", "--pairs", "10", "--min-degree", "1",
                     "--hub-floor", "10", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert [r["cap"] for r in report["sweep"]] == [5, None] and len(report["pairs"]) == 10
    printed = capsys.readouterr().out
    assert "Cap sweep" in printed and "Big U" in printed


def test_parse_cap(mod):
    assert mod.parse_cap("none") is None and mod.parse_cap("2000") == 2000
