"""Build the offline people graph (SQLite) from an extracted truthy-dump slice.

Usage:
    uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
        --out data/wikidata/people_graph.sqlite --dump-meta data/wikidata/raw/<dump>.meta.json \
        [--workers N]

Input: N-Triples lines produced by scripts/extract_people_slice.sh (P31, allowlisted
relationships, en/es/mul rdfs:label). Output tables:

    entity(qid INTEGER PK, label_en, label_es, is_human, kind, out_degree, in_degree)
    edge(subject INTEGER, pid TEXT, object INTEGER)        -- original Wikidata direction
    entity_type(qid INTEGER, class_qid INTEGER)            -- P31 for entities in the graph
    meta(key TEXT PK, value TEXT)

Kept: statements whose SUBJECT is a human (P31 Q5), which excludes e.g. organizations that are
"member of" other bodies; plus, for OBJECT_SIDE_PIDS only, statements whose OBJECT is a human.
Those properties are recorded on the work or organization and point at the person (a film's
cast member, a song's performer, a company's founder). P50 "author" is deliberately not among
them: it is dominated by scholarly-article co-authorship and would swamp the graph.
QIDs are stored as integers (Q42 -> 42). Edges are deduplicated on (subject, pid, object).

Labels: label_en / label_es hold the first en / es label; where an entity has none, its "mul"
label fills in. mul is Wikidata's language-independent default label, and since 2024 many items
carry their name only there (Marie Curie, Donald Trump and Shakira have no en label in the
2026-11 dump), so without the fallback ~1.1M graph nodes, many famous people, had no label.

How (two passes; ~95% of the slice is P31 and labels of entities that never enter the graph):
  1. scan every line for relationship edges and the set of humans (P31 Q5);
  2. in numpy: apply the human rule, deduplicate, derive the node set, degrees and kinds;
  3. scan again for labels and P31 types of graph nodes only (first occurrence per language wins);
  4. write SQLite once. Both scans run in parallel over newline-aligned byte ranges; results are
     merged in file order, so the output does not depend on the number of workers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path

WD = "http://www.wikidata.org/entity/Q"
WDT = "http://www.wikidata.org/prop/direct/"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
LABEL_LANGS = (b'"@en', b'"@es', b'"@mul')  # mul fills in en and es where those are missing

RELATION_PIDS = (
    "P26", "P22", "P25", "P3373", "P40", "P451", "P1038",      # family
    "P1066", "P802", "P184", "P185",                           # mentorship
    "P108", "P69", "P463", "P54", "P112",                      # institutional
    "P161", "P57", "P58", "P86", "P175",                       # works
    "P39", "P19", "P166", "P102", "P737",                      # weak groups (off by default)
)
WORK_PIDS = ("P161", "P57", "P58", "P86", "P175")             # work -> person
OBJECT_SIDE_PIDS = WORK_PIDS + ("P112",)                       # kept when the OBJECT is a human
INSTITUTION_PIDS = ("P108", "P69", "P463", "P54", "P102")      # person -> organization
HUMAN = 5

UCHAR8 = re.compile(r"\\U([0-9A-Fa-f]{8})")

# Byte-level tokens (the scans work on bytes: no regex, no per-line decode).
_E = WD.encode()                       # b"http://www.wikidata.org/entity/Q"
_ENT = b"<" + _E                       # subject / object IRI prefix
_DIRECT = b"<" + WDT.encode()          # b"<http://www.wikidata.org/prop/direct/"
_LABEL = b"<" + LABEL.encode() + b">"
_P31 = _DIRECT + b"P31>"
_Q5 = _ENT + b"5>"
_PID_CODE = {p.encode(): i for i, p in enumerate(RELATION_PIDS)}

BLOCK = 64 << 20          # bytes read per block in a scan
SMALL_INPUT = 64 << 20    # below this, scan in-process (no worker pool)


def unescape(lit: str) -> str:
    """Decode N-Triples string escapes (\\t \\n \\" \\\\ \\uXXXX \\UXXXXXXXX)."""
    if "\\" not in lit:
        return lit
    lit = UCHAR8.sub(lambda m: chr(int(m.group(1), 16)), lit)
    try:
        return json.loads(f'"{lit}"')
    except json.JSONDecodeError:
        return lit


# ---- scanning -----------------------------------------------------------------------------------


def _lines(path: str, start: int, end: int):
    """Yield the complete lines (bytes, newline stripped) in [start, end). Boundaries are line starts."""
    with open(path, "rb") as fh:
        fh.seek(start)
        remaining, carry = end - start, b""
        while remaining > 0:
            block = fh.read(min(BLOCK, remaining))
            if not block:
                break
            remaining -= len(block)
            parts = (carry + block).split(b"\n")
            carry = parts.pop()
            yield from parts
        if carry:
            yield carry


def _split_triple(line: bytes):
    """(subject QID int, predicate IRI bytes, object term bytes) or None if not '<Q..> <p> o .'"""
    if not line.startswith(_ENT):
        return None
    parts = line.split(b" ", 2)
    if len(parts) != 3:
        return None
    s_term, p_term, rest = parts
    rest = rest.rstrip()
    if not (s_term.endswith(b">") and p_term.startswith(b"<") and rest.endswith(b" .")):
        return None
    digits = s_term[len(_ENT):-1]
    if not digits.isdigit():
        return None
    return int(digits), p_term, rest[:-2].rstrip()


def _object_qid(term: bytes) -> int | None:
    if term.startswith(_ENT) and term.endswith(b">"):
        digits = term[len(_ENT):-1]
        if digits.isdigit():
            return int(digits)
    return None


def scan_edges(path: str, start: int, end: int) -> dict:
    """Pass 1 over one byte range: relationship edges and humans. Arrays travel as bytes."""
    subj, obj, code, humans = array("i"), array("i"), array("b"), array("i")
    stats = {"lines": 0, "unparsed": 0, "types": 0, "edges": 0, "labels": 0, "skipped": 0}
    for line in _lines(path, start, end):
        if not line.strip():
            continue
        stats["lines"] += 1
        t = _split_triple(line)
        if t is None:
            # Lines with a non-QID subject (e.g. owl:/wikiba.se schema lines) count as skipped.
            stats["skipped" if line.startswith(b"<") and line.rstrip().endswith(b" .") else "unparsed"] += 1
            continue
        s, p, o_term = t
        if p == _LABEL:
            if o_term.startswith(b'"') and o_term.endswith(LABEL_LANGS):
                stats["labels"] += 1
            else:
                stats["skipped"] += 1
            continue
        if not p.startswith(_DIRECT):
            stats["skipped"] += 1
            continue
        o = _object_qid(o_term)
        if o is None:
            stats["skipped"] += 1
        elif p == _P31:
            stats["types"] += 1
            if o == HUMAN:
                humans.append(s)
        else:
            c = _PID_CODE.get(p[len(_DIRECT):-1])
            if c is None:
                stats["skipped"] += 1
            else:
                subj.append(s); obj.append(o); code.append(c)
                stats["edges"] += 1
    return {"subj": subj.tobytes(), "obj": obj.tobytes(), "code": code.tobytes(),
            "humans": humans.tobytes(), "stats": stats}


_NODE_MASK = None  # per-worker view of the shared node bitmap


def _init_labels(shm_name: str | None, mask: bytes | None) -> None:
    global _NODE_MASK
    if shm_name is not None:
        from multiprocessing import shared_memory

        shm = shared_memory.SharedMemory(name=shm_name)
        _NODE_MASK = (shm, shm.buf)  # keep the handle alive with the view
    else:
        _NODE_MASK = (None, mask)


def scan_labels(path: str, start: int, end: int) -> dict:
    """Pass 2 over one byte range: en/es/mul labels and P31 classes of graph nodes only."""
    mask = _NODE_MASK[1]
    size = len(mask)
    labels: dict[tuple[int, str], str] = {}
    tq, tc = array("i"), array("i")
    for line in _lines(path, start, end):
        t = _split_triple(line)
        if t is None:
            continue
        s, p, o_term = t
        if s >= size or not mask[s]:
            continue
        if p == _LABEL:
            if o_term.startswith(b'"') and o_term.endswith(LABEL_LANGS):
                at = o_term.rindex(b'"@')
                key = (s, o_term[at + 2:].decode())
                if key not in labels:  # first occurrence wins (as the SQL LIMIT 1 did)
                    labels[key] = unescape(o_term[1:at].decode("utf-8", errors="replace"))
        elif p == _P31:
            o = _object_qid(o_term)
            if o is not None:
                tq.append(s); tc.append(o)
    return {"labels": [(q, lang, text) for (q, lang), text in labels.items()],
            "tq": tq.tobytes(), "tc": tc.tobytes()}


def ranges(path: Path, parts: int) -> list[tuple[int, int]]:
    """Split the file into ``parts`` contiguous byte ranges that start at line starts."""
    size = path.stat().st_size
    cuts = [0]
    with open(path, "rb") as fh:
        for k in range(1, parts):
            fh.seek(max(size * k // parts, cuts[-1]))
            fh.readline()
            pos = fh.tell()
            if cuts[-1] < pos < size:
                cuts.append(pos)
    cuts.append(size)
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if b > a]


def run_scan(fn, path: Path, workers: int, initializer=None, initargs=()):
    """Run ``fn`` over byte ranges; yields results in file order as they become available."""
    if workers <= 1 or path.stat().st_size < SMALL_INPUT:
        if initializer:
            initializer(*initargs)
        for a, b in ranges(path, 1):
            yield fn(str(path), a, b)
        return
    from concurrent.futures import ProcessPoolExecutor

    chunks = ranges(path, workers * 4)  # smaller chunks balance uneven ranges
    with ProcessPoolExecutor(workers, initializer=initializer, initargs=initargs) as pool:
        yield from pool.map(fn, [str(path)] * len(chunks), [a for a, _ in chunks], [b for _, b in chunks])


# ---- assembly -----------------------------------------------------------------------------------


def assemble(results: list[dict]):
    """Concatenate pass-1 results; apply the human rule; dedupe. Returns (edges, humans, stats)."""
    import numpy as np

    cat = lambda key, dt: np.concatenate([np.frombuffer(r[key], dtype=dt) for r in results]) \
        if results else np.zeros(0, dtype=dt)
    subj, obj, code = cat("subj", np.int32), cat("obj", np.int32), cat("code", np.int8)
    humans = np.unique(cat("humans", np.int32))
    stats = {k: sum(r["stats"][k] for r in results) for k in results[0]["stats"]} if results else {}

    is_human = lambda q: np.isin(q, humans, assume_unique=False)
    objside = np.zeros(len(RELATION_PIDS), dtype=bool)
    objside[[RELATION_PIDS.index(p) for p in OBJECT_SIDE_PIDS]] = True
    keep = (subj != obj) & (is_human(subj) | (objside[code] & is_human(obj)))
    s, c, o = subj[keep].astype(np.int64), code[keep].astype(np.int64), obj[keep].astype(np.int64)
    # Dedupe on one packed int64 key: subject (30 bits) | PID code (5 bits) | object (28 bits).
    # QIDs are ~1.4e8 today (< 2^28 = 2.7e8); fail loudly rather than corrupt edges if that changes.
    if int(o.max(initial=0)) >= 1 << 28 or int(s.max(initial=0)) >= 1 << 30 or len(RELATION_PIDS) > 32:
        raise ValueError("QID or PID count too large for the edge key packing; widen the key")
    key = np.unique((s << 33) | (c << 28) | o)
    edges = (key >> 33, (key >> 28) & 31, key & ((1 << 28) - 1))
    return edges, humans, stats


def node_table(edges, humans):
    """Node QIDs (sorted), is_human, kind, out/in degree, from the deduplicated edges."""
    import numpy as np

    s, c, o = edges
    nodes = np.unique(np.concatenate([s, o]))
    si, oi = np.searchsorted(nodes, s), np.searchsorted(nodes, o)
    n = len(nodes)
    out_deg = np.bincount(si, minlength=n)
    in_deg = np.bincount(oi, minlength=n)
    human = np.isin(nodes, humans)

    def marked(index, pids):
        m = np.zeros(n, dtype=bool)
        sel = np.isin(c, [RELATION_PIDS.index(p) for p in pids])
        m[index[sel]] = True
        return m

    # Precedence: person, then place if birthplace links are at least half its links, then
    # organization, work, position, place, other. The majority rule exists because cities also
    # collect a few employer/school/team links (Berlin: 42,278 births, 15 employees); by plain
    # precedence they became organizations and escaped the place hub cap.
    births = np.bincount(oi[c == RELATION_PIDS.index("P19")], minlength=n)
    birthplace_majority = 2 * births >= out_deg + in_deg
    kind = np.full(n, "other", dtype=object)
    for name, mask in reversed([
        ("place", (births > 0) & birthplace_majority),
        ("organization", marked(oi, INSTITUTION_PIDS) | marked(si, ("P112",))),
        ("work", marked(si, WORK_PIDS)),
        ("position", marked(oi, ("P39",))),
        ("place", births > 0),
    ]):
        kind[mask] = name
    kind[human] = "person"
    return nodes, human, kind, out_deg, in_deg


# ---- SQLite -------------------------------------------------------------------------------------


class Writer:
    """Writes the SQLite file in steps, so pass 2 can stream labels into it."""

    def __init__(self, path: Path) -> None:
        if path.exists():
            path.unlink()
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = FILE;
            PRAGMA cache_size = -1000000;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE edge(subject INT,pid TEXT,object INT);
            CREATE TABLE entity (
              qid INTEGER PRIMARY KEY, label_en TEXT, label_es TEXT,
              is_human INTEGER NOT NULL, kind TEXT NOT NULL,
              out_degree INTEGER NOT NULL DEFAULT 0, in_degree INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE entity_type(qid INT,class_qid INT);
            CREATE TEMP TABLE label_mul(qid INTEGER PRIMARY KEY, label TEXT NOT NULL);
        """)

    def step(self, name: str, fn) -> None:
        t0 = time.time()
        fn()
        self.db.commit()
        print(f"  {name}: {time.time() - t0:.1f}s", flush=True)

    def graph(self, edges, nodes, human, kind, out_deg, in_deg) -> None:
        s, c, o = edges
        self.step("edges", lambda: self.db.executemany(
            "INSERT INTO edge VALUES (?, ?, ?)",
            zip(s.tolist(), (RELATION_PIDS[i] for i in c.tolist()), o.tolist())))
        self.step("entities", lambda: self.db.executemany(
            "INSERT INTO entity (qid, is_human, kind, out_degree, in_degree) VALUES (?, ?, ?, ?, ?)",
            zip(nodes.tolist(), human.astype(int).tolist(), kind.tolist(), out_deg.tolist(), in_deg.tolist())))

    def labels(self, rows) -> int:
        """Rows in FILE order; the first label per (qid, language) wins.

        mul labels are staged and applied only in finish(): an entity's lines can be split across
        chunks, so filling in from mul now could beat an en/es label that arrives later."""
        en = [(t, q) for q, lang, t in rows if lang == "en"]
        es = [(t, q) for q, lang, t in rows if lang == "es"]
        mul = [(q, t) for q, lang, t in rows if lang == "mul"]
        self.db.executemany("UPDATE entity SET label_en = ? WHERE qid = ? AND label_en IS NULL", en)
        self.db.executemany("UPDATE entity SET label_es = ? WHERE qid = ? AND label_es IS NULL", es)
        self.db.executemany("INSERT OR IGNORE INTO label_mul VALUES (?, ?)", mul)
        return len(en) + len(es) + len(mul)

    def apply_mul_fallback(self) -> None:
        """Fill missing en / es labels from the entity's mul label."""
        for col in ("label_en", "label_es"):
            self.db.execute(
                f"UPDATE entity SET {col} = (SELECT label FROM label_mul m WHERE m.qid = entity.qid) "
                f"WHERE {col} IS NULL AND qid IN (SELECT qid FROM label_mul)")

    def finish(self, types) -> None:
        self.step("mul label fallback", self.apply_mul_fallback)
        self.step("entity types", lambda: self.db.executemany("INSERT INTO entity_type VALUES (?, ?)", types))
        self.step("indexes", lambda: self.db.executescript(
            "CREATE INDEX edge_subject ON edge(subject); CREATE INDEX edge_object ON edge(object);"
            "CREATE INDEX entity_type_qid ON entity_type(qid);"
            "CREATE INDEX entity_label_en ON entity(label_en COLLATE NOCASE) WHERE is_human;"
            "CREATE INDEX entity_label_es ON entity(label_es COLLATE NOCASE) WHERE is_human;"))
        self.db.close()


def write_meta(db_path: Path, args: argparse.Namespace, stats: dict[str, int]) -> dict:
    db = sqlite3.connect(db_path)
    counts = {
        "entities": db.execute("SELECT COUNT(*) FROM entity").fetchone()[0],
        "humans": db.execute("SELECT COUNT(*) FROM entity WHERE is_human").fetchone()[0],
        "edges": db.execute("SELECT COUNT(*) FROM edge").fetchone()[0],
        "edges_by_pid": dict(db.execute("SELECT pid, COUNT(*) FROM edge GROUP BY pid ORDER BY pid")),
        "entities_by_kind": dict(db.execute("SELECT kind, COUNT(*) FROM entity GROUP BY kind")),
        "missing_label": db.execute(
            "SELECT COUNT(*) FROM entity WHERE label_en IS NULL AND label_es IS NULL").fetchone()[0],
    }
    dump_meta = json.loads(Path(args.dump_meta).read_text()) if args.dump_meta else {}
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_slice": str(args.slice),
        "dump": dump_meta,
        "relation_pids": list(RELATION_PIDS),
        "subject_filter": "P31 Q5 (human); object is human for " + " ".join(OBJECT_SIDE_PIDS),
        "kind_rule": "person > place if P19 links >= half of all links > organization > work "
                     "> position > place > other",
        "label_languages": ["en", "es"],
        "label_fallback": "mul (Wikidata's language-independent label) fills missing en / es",
        "load_stats": stats,
        "counts": counts,
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    db.commit()
    db.close()
    return meta


def build(slice_path: Path, out_tmp: Path, workers: int) -> dict[str, int]:
    import numpy as np

    t0 = time.time()
    print(f"pass 1: edges and humans ({workers} worker(s)) ...", flush=True)
    edges, humans, stats = assemble(list(run_scan(scan_edges, slice_path, workers)))
    print(f"  {stats}  ({time.time() - t0:.0f}s)", flush=True)
    nodes, human, kind, out_deg, in_deg = node_table(edges, humans)
    print(f"  {len(edges[0]):,} edges, {len(nodes):,} nodes, {int(human.sum()):,} humans in the graph", flush=True)

    print("writing SQLite ...", flush=True)
    writer = Writer(out_tmp)
    writer.graph(edges, nodes, human, kind, out_deg, in_deg)
    del edges, human, kind, out_deg, in_deg

    t1 = time.time()
    print("pass 2: labels and types of graph nodes (streamed into SQLite) ...", flush=True)
    mask = np.zeros(int(nodes.max(initial=0)) + 1, dtype=np.uint8)
    mask[nodes] = 1
    del nodes
    shm = None
    if workers > 1 and slice_path.stat().st_size >= SMALL_INPUT:
        from multiprocessing import shared_memory

        shm = shared_memory.SharedMemory(create=True, size=max(1, len(mask)))
        shm.buf[: len(mask)] = mask.tobytes()
        init = (shm.name, None)
    else:
        init = (None, mask.tobytes())
    n_labels, tq_parts, tc_parts = 0, [], []
    try:
        for r in run_scan(scan_labels, slice_path, workers, _init_labels, init):
            n_labels += writer.labels(r["labels"])
            tq_parts.append(np.frombuffer(r["tq"], dtype=np.int32))
            tc_parts.append(np.frombuffer(r["tc"], dtype=np.int32))
    finally:
        if shm is not None:
            shm.close()
            shm.unlink()
    tq = np.concatenate(tq_parts).astype(np.int64) if tq_parts else np.zeros(0, dtype=np.int64)
    tc = np.concatenate(tc_parts).astype(np.int64) if tc_parts else np.zeros(0, dtype=np.int64)
    pairs = np.unique((tq << 32) | tc)
    print(f"  {n_labels:,} label rows, {len(pairs):,} types  ({time.time() - t1:.0f}s)", flush=True)
    writer.finish(zip((pairs >> 32).tolist(), (pairs & 0xFFFFFFFF).tolist()))
    return stats


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slice", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/wikidata/people_graph.sqlite"))
    ap.add_argument("--dump-meta", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=default_workers(),
                    help="parallel scan processes (default: half the cores, at most 8; 1 = in-process)")
    args = ap.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".building")
    t0 = time.time()
    stats = build(args.slice, tmp, args.workers)
    meta = write_meta(tmp, args, stats)
    if args.out.exists():
        args.out.unlink()
    tmp.rename(args.out)
    print(json.dumps(meta["counts"], indent=2))
    print(f"wrote {args.out} in {time.time() - t0:.0f}s")
    # Precompute the in-memory adjacency index used for exact path searches, so the first
    # script/server that opens the graph does not pay for it.
    t1 = time.time()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.graph.store import IndexedPeopleGraph

    indexed = IndexedPeopleGraph(args.out)
    print(f"adjacency index: {indexed.cache_dir.name}/ "
          f"({indexed.cache_bytes / 2**20:.0f} MiB, {time.time() - t1:.0f}s)")
    indexed.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
