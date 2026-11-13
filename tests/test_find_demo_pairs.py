"""Seed resolution and the end-to-end run of scripts/find_demo_pairs.py on a synthetic graph."""

import importlib.util
import json

import pytest

from .graph_helpers import build_people_db

# Two humans share the label "Ada Test"; the better-connected one must win and be flagged.
PEOPLE = {"Q900001": "Ada Test", "Q900002": "Ada Test", "Q900003": "Bob Test", "Q900004": "Cy Test",
          "Q900005": "Di Test"}
ORGS = {"Q910001": "Test University"}
TRIPLES = [
    ("Q900001", "P26", "Q900003"), ("Q900001", "P69", "Q910001"), ("Q900003", "P1066", "Q900004"),
    ("Q900004", "P69", "Q910001"), ("Q900005", "P69", "Q910001"), ("Q900002", "P69", "Q910001"),
    ("Q900001", "P1066", "Q900005"),
]


@pytest.fixture(scope="module")
def mod(request):
    root = request.config.rootpath
    spec = importlib.util.spec_from_file_location("find_demo_pairs", root / "scripts" / "find_demo_pairs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def db(request, tmp_path_factory):
    return build_people_db(request.config.rootpath, tmp_path_factory.mktemp("seeds"), PEOPLE, ORGS, TRIPLES)


def test_read_seed_lines_strips_comments_and_reads_groups(mod, tmp_path):
    f = tmp_path / "seeds.txt"
    f.write_text("# header\n\nAda Test   # inline\n[recent]  # comment\n  Q900003\n#only comment\nCy   Test\n"
                 "[ old ]\nDi Test\n", encoding="utf-8")
    assert mod.read_seed_lines(f) == [("Ada Test", "default"), ("Q900003", "recent"), ("Cy Test", "recent"),
                                      ("Di Test", "old")]


def test_seed_pairs_all_or_across_groups(mod):
    res = [{"qid": "Q1", "group": "a"}, {"qid": "Q2", "group": "a"}, {"qid": "Q3", "group": "b"},
           {"qid": None, "group": "b"}, {"qid": "Q1", "group": "b"}]  # Q1 listed in both groups
    assert mod.seed_pairs(res, None) == [("Q1", "Q2"), ("Q1", "Q3"), ("Q2", "Q3")]
    assert mod.seed_pairs(res, ("a", "b")) == [("Q1", "Q3"), ("Q2", "Q3"), ("Q2", "Q1")]


def test_the_shipped_seed_file_parses(mod, request):
    entries = mod.read_seed_lines(request.config.rootpath / "config" / "demo_seeds.txt")
    groups = {g for _, g in entries}
    assert groups == {"historical", "recent"}
    names = [e for e, _ in entries]
    assert len(names) == len(set(names)) and len(names) > 100


def test_resolve_exact_label_qid_spanish_and_missing(mod, db):
    res = {r["entry"]: r for r in mod.resolve_seeds(db, [(e, "default") for e in (
        "ada test", "Q900003", "ES Cy Test", "Ada", "Q910001", "Q999")])}
    ada = res["ada test"]
    assert ada["qid"] == "Q900001" and ada["matches"] == 2 and ada["others"] == ["Q900002"] and ada["via"] == "en"
    assert res["Q900003"]["qid"] == "Q900003" and res["Q900003"]["via"] == "qid"
    assert res["ES Cy Test"]["qid"] == "Q900004" and res["ES Cy Test"]["via"] == "es"
    assert res["Ada"]["qid"] is None  # exact match only, never a prefix guess
    assert res["Q910001"]["qid"] is None  # an organisation is not a person
    assert res["Q999"]["qid"] is None


def test_resolve_only_reports_and_fails_on_missing(mod, db, tmp_path, capsys):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("Ada Test\nBob Test\n", encoding="utf-8")
    assert mod.main(["--db", str(db), "--seeds", str(seeds), "--resolve-only"]) == 0
    err = capsys.readouterr().err
    assert "AMBIGUOUS: 2 humans" in err and "low degree" in err and "Bob Test → Q900003" in err
    seeds.write_text("Ada Test\nNobody Here\n", encoding="utf-8")
    assert mod.main(["--db", str(db), "--seeds", str(seeds), "--resolve-only"]) == 1
    assert "Nobody Here: not found" in capsys.readouterr().err


def test_seed_run_evaluates_pairs_and_writes_rows(mod, db, tmp_path):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("Bob Test\nDi Test\nCy Test\n", encoding="utf-8")
    out = tmp_path / "rows.json"
    assert mod.main(["--db", str(db), "--seeds", str(seeds), "--out", str(out)]) == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    pairs = {frozenset((r["source"], r["target"])) for r in rows}
    # Bob-Cy are 1 hop apart (filtered out: demo pairs need 2+ hops); the other two pairs remain.
    assert pairs == {frozenset(("Q900003", "Q900005")), frozenset(("Q900005", "Q900004"))}
    assert all(r["live_found"] in (True, False) and "score" in r for r in rows)


def test_cross_run_pairs_only_across_groups(mod, db, tmp_path):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("[recent]\nBob Test\nCy Test\n[historical]\nDi Test\n", encoding="utf-8")
    out = tmp_path / "rows.json"
    assert mod.main(["--db", str(db), "--seeds", str(seeds), "--cross", "recent", "historical",
                     "--out", str(out)]) == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert {(r["source"], r["target"]) for r in rows} == {("Q900003", "Q900005"), ("Q900004", "Q900005")}
    with pytest.raises(SystemExit):  # unknown group: a clear error, not an empty run
        mod.main(["--db", str(db), "--seeds", str(seeds), "--cross", "recent", "nope"])
