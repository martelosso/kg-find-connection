"""Parallel pair evaluation (src/pairwork.py) and the memory-mapped adjacency cache it relies on.

These tests start real worker processes: results must equal the in-process run, in input order.
"""

import importlib.util
import itertools
import json

import numpy as np
import pytest

from src.graph.store import IndexedPeopleGraph
from src.pairwork import Context, benchmark_pair, demo_pair_row, hub_sweep_pair, map_pairs

from .graph_helpers import build_people_db, random_people_graph


@pytest.fixture(scope="module")
def db(request, tmp_path_factory):
    people, orgs, triples = random_people_graph(seed=21, n_people=50, n_orgs=6, n_edges=180)
    path = build_people_db(request.config.rootpath, tmp_path_factory.mktemp("pairwork"), people, orgs, triples)
    g = IndexedPeopleGraph(path)  # writes the shared cache once, like the builder does
    g.close()
    return path, sorted(people)


def _strip_times(x):
    """Timings differ between runs; everything else must be identical."""
    if isinstance(x, dict):
        return {k: _strip_times(v) for k, v in x.items() if not k.endswith("seconds") and k not in ("exact_s", "inv_s")}
    if isinstance(x, list):
        return [_strip_times(v) for v in x]
    return x


@pytest.mark.parametrize("task,kwargs", [
    (demo_pair_row, {"max_hops": 4}),
    (benchmark_pair, {}),
    (hub_sweep_pair, {"caps": [5, None], "groups": None, "hub_floor": 5}),
])
def test_parallel_results_equal_in_process_results_in_order(db, task, kwargs):
    path, people = db
    pairs = list(itertools.combinations(people[:9], 2))
    one = list(map_pairs(path, task, pairs, 1, **kwargs))
    many = list(map_pairs(path, task, pairs, 3, **kwargs))
    assert len(one) == len(pairs) and _strip_times(many) == _strip_times(one)
    assert any(r is not None for r in one)  # the fixture really has connections


def test_breaking_out_early_cancels_the_rest(db):
    path, people = db
    pairs = list(itertools.combinations(people[:12], 2))
    gen = map_pairs(path, benchmark_pair, pairs, 2)
    first = [next(gen) for _ in range(3)]
    gen.close()  # must not hang waiting for the remaining pairs
    assert _strip_times(first) == _strip_times(list(map_pairs(path, benchmark_pair, pairs[:3], 1)))


def test_cache_is_memory_mapped_read_only_and_shared_layout(db):
    path, _ = db
    g = IndexedPeopleGraph(path)
    try:
        assert g.cache_path.is_file() and (g.cache_dir / "nbr.npy").is_file()
        import mmap

        # A read-only ndarray view on a memmap on the OS mapping: shared, never copied per process.
        assert not g._nbr.flags.writeable
        assert isinstance(g._nbr.base, np.memmap) and isinstance(g._nbr.base.base, mmap.mmap)
        manifest = json.loads(g.cache_path.read_text(encoding="utf-8"))
        assert set(manifest) == {"fingerprint", "pids"} and json.loads(manifest["fingerprint"])["format"] >= 3
        assert g.cache_bytes > 0
    finally:
        g.close()


def test_damaged_array_file_is_rebuilt_not_trusted(db, tmp_path):
    import shutil

    path, people = db
    copy = tmp_path / "copy.sqlite"
    shutil.copy(path, copy)
    g = IndexedPeopleGraph(copy)
    expected = {q: g.incident(q, frozenset(g._pid_names)) for q in people[:10]}
    cache = g.cache_dir
    g.close()
    (cache / "offsets.npy").write_bytes(b"\x93NUMPY broken")
    again = IndexedPeopleGraph(copy)
    try:
        assert {q: again.incident(q, frozenset(again._pid_names)) for q in people[:10]} == expected
    finally:
        again.close()


def test_context_reuses_an_open_graph(db):
    path, _ = db
    g = IndexedPeopleGraph(path)
    try:
        assert Context(path, graph=g).graph is g
    finally:
        g.close()


def test_scripts_accept_workers(request, db, tmp_path):
    """The three prep scripts run end to end with --workers 2 and give the in-process answer."""
    root = request.config.rootpath
    path, people = db

    def load(name):
        spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    hubs = load("analyze_hubs")
    outs = []
    for w in ("1", "2"):
        out = tmp_path / f"hubs{w}.json"
        assert hubs.main(["--db", str(path), "--caps", "5", "none", "--pairs", "12", "--min-degree", "1",
                          "--hub-floor", "5", "--workers", w, "--out", str(out)]) == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["workers"] == int(w)  # --workers really reaches the sweep
        outs.append([{k: v for k, v in r.items() if k not in ("median_s", "max_s")} for r in report["sweep"]])
    assert outs[0] == outs[1]

    seeds = tmp_path / "seeds.txt"
    seeds.write_text("\n".join(people[:6]) + "\n", encoding="utf-8")
    fdp = load("find_demo_pairs")
    rows = []
    for w in ("1", "2"):
        out = tmp_path / f"pairs{w}.json"
        assert fdp.main(["--db", str(path), "--seeds", str(seeds), "--workers", w, "--out", str(out)]) == 0
        rows.append(_strip_times(json.loads(out.read_text(encoding="utf-8"))))
    assert rows[0] == rows[1]

    bench = load("benchmark_investigations")
    assert bench.main(["--db", str(path), "--pairs", "4", "--min-degree", "1", "--max-degree", "1000",
                       "--workers", "2"]) == 0
