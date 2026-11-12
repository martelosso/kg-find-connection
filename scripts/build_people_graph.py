"""Build the offline people graph (SQLite) from an extracted truthy-dump slice.

Usage:
    uv run python scripts/build_people_graph.py data/wikidata/slice/slice.nt \
        --out data/wikidata/people_graph.sqlite --dump-meta data/wikidata/raw/<dump>.meta.json

Input: N-Triples lines produced by scripts/extract_people_slice.sh (P31, allowlisted
relationships, en/es rdfs:label). Output tables:

    entity(qid INTEGER PK, label_en, label_es, is_human, kind, out_degree, in_degree)
    edge(subject INTEGER, pid TEXT, object INTEGER)        -- original Wikidata direction
    entity_type(qid INTEGER, class_qid INTEGER)            -- P31 for entities in the graph
    meta(key TEXT PK, value TEXT)

Only statements whose SUBJECT is a human (P31 Q5) are kept: every allowlisted property is
person-centric, and this excludes e.g. organizations that are "member of" other bodies.
QIDs are stored as integers (Q42 -> 42). Edges are deduplicated on (subject, pid, object).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WD = "http://www.wikidata.org/entity/Q"
WDT = "http://www.wikidata.org/prop/direct/"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

RELATION_PIDS = ("P26", "P22", "P25", "P3373", "P1066", "P108", "P69", "P463", "P39")
INSTITUTION_PIDS = ("P108", "P69", "P463")
HUMAN = 5

TRIPLE = re.compile(r'^<([^>]+)> <([^>]+)> (.+) \.\s*$')
LANG_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"@(en|es)$')
UCHAR8 = re.compile(r"\\U([0-9A-Fa-f]{8})")

BATCH = 200_000


def unescape(lit: str) -> str:
    """Decode N-Triples string escapes (\\t \\n \\" \\\\ \\uXXXX \\UXXXXXXXX)."""
    if "\\" not in lit:
        return lit
    lit = UCHAR8.sub(lambda m: chr(int(m.group(1), 16)), lit)
    try:
        return json.loads(f'"{lit}"')
    except json.JSONDecodeError:
        return lit


def qid_int(iri: str) -> int | None:
    if iri.startswith(WD):
        tail = iri[len(WD):]
        if tail.isdigit():
            return int(tail)
    return None


def connect(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = FILE;
        PRAGMA cache_size = -1000000;
        CREATE TABLE raw_type  (s INTEGER, o INTEGER);
        CREATE TABLE raw_edge  (s INTEGER, pid TEXT, o INTEGER);
        CREATE TABLE raw_label (qid INTEGER, lang TEXT, text TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return db


def load(db: sqlite3.Connection, slice_path: Path) -> dict[str, int]:
    stats = {"lines": 0, "unparsed": 0, "types": 0, "edges": 0, "labels": 0, "skipped": 0}
    types, edges, labels = [], [], []

    def flush() -> None:
        db.executemany("INSERT INTO raw_type VALUES (?, ?)", types)
        db.executemany("INSERT INTO raw_edge VALUES (?, ?, ?)", edges)
        db.executemany("INSERT INTO raw_label VALUES (?, ?, ?)", labels)
        types.clear(); edges.clear(); labels.clear()

    t0 = time.time()
    with open(slice_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stats["lines"] += 1
            m = TRIPLE.match(line)
            if not m:
                stats["unparsed"] += 1
                continue
            s_iri, p_iri, o_term = m.groups()
            s = qid_int(s_iri)
            if s is None:
                stats["skipped"] += 1
                continue
            if p_iri == LABEL:
                lm = LANG_LITERAL.match(o_term)
                if lm:
                    labels.append((s, lm.group(2), unescape(lm.group(1))))
                    stats["labels"] += 1
                else:
                    stats["skipped"] += 1
            elif p_iri.startswith(WDT) and o_term.startswith("<"):
                o = qid_int(o_term[1:-1])
                pid = p_iri[len(WDT):]
                if o is None:
                    stats["skipped"] += 1
                elif pid == "P31":
                    types.append((s, o)); stats["types"] += 1
                elif pid in RELATION_PIDS:
                    edges.append((s, pid, o)); stats["edges"] += 1
                else:
                    stats["skipped"] += 1
            else:
                stats["skipped"] += 1
            if len(types) + len(edges) + len(labels) >= BATCH:
                flush()
            if stats["lines"] % 10_000_000 == 0:
                print(f"  {stats['lines']:,} lines  {time.time() - t0:.0f}s", flush=True)
    flush()
    db.commit()
    return stats


def finalize(db: sqlite3.Connection) -> None:
    steps = [
        ("humans", "CREATE TABLE human AS SELECT DISTINCT s AS qid FROM raw_type WHERE o = ?", (HUMAN,)),
        ("human pk", "CREATE UNIQUE INDEX human_qid ON human(qid)", ()),
        ("edges (human subjects, deduped)",
         """CREATE TABLE edge AS
              SELECT DISTINCT e.s AS subject, e.pid AS pid, e.o AS object
              FROM raw_edge e JOIN human h ON h.qid = e.s
              WHERE e.s <> e.o""", ()),
        ("edge indexes", "CREATE INDEX edge_subject ON edge(subject); CREATE INDEX edge_object ON edge(object)", ()),
        ("entity ids",
         "CREATE TABLE node AS SELECT subject AS qid FROM edge UNION SELECT object FROM edge", ()),
        ("entity table",
         """CREATE TABLE entity (
              qid INTEGER PRIMARY KEY, label_en TEXT, label_es TEXT,
              is_human INTEGER NOT NULL, kind TEXT NOT NULL,
              out_degree INTEGER NOT NULL DEFAULT 0, in_degree INTEGER NOT NULL DEFAULT 0)""", ()),
        ("label index", "CREATE INDEX raw_label_qid ON raw_label(qid, lang)", ()),
        ("populate entity",
         f"""INSERT INTO entity (qid, label_en, label_es, is_human, kind)
             SELECT n.qid,
                    (SELECT text FROM raw_label WHERE qid = n.qid AND lang = 'en' LIMIT 1),
                    (SELECT text FROM raw_label WHERE qid = n.qid AND lang = 'es' LIMIT 1),
                    EXISTS (SELECT 1 FROM human WHERE qid = n.qid),
                    CASE
                      WHEN EXISTS (SELECT 1 FROM human WHERE qid = n.qid) THEN 'person'
                      WHEN EXISTS (SELECT 1 FROM edge WHERE object = n.qid
                                   AND pid IN ({",".join(f"'{p}'" for p in INSTITUTION_PIDS)}))
                        THEN 'organization'
                      WHEN EXISTS (SELECT 1 FROM edge WHERE object = n.qid AND pid = 'P39')
                        THEN 'position'
                      ELSE 'other'
                    END
             FROM node n""", ()),
        ("degrees",
         """UPDATE entity SET
              out_degree = (SELECT COUNT(*) FROM edge WHERE subject = entity.qid),
              in_degree  = (SELECT COUNT(*) FROM edge WHERE object  = entity.qid)""", ()),
        ("entity types",
         """CREATE TABLE entity_type AS
              SELECT DISTINCT t.s AS qid, t.o AS class_qid
              FROM raw_type t JOIN entity e ON e.qid = t.s""", ()),
        ("entity type index", "CREATE INDEX entity_type_qid ON entity_type(qid)", ()),
        ("drop raw", "DROP TABLE raw_type; DROP TABLE raw_edge; DROP TABLE raw_label; DROP TABLE node; DROP TABLE human", ()),
    ]
    for name, sql, params in steps:
        t0 = time.time()
        if params:
            db.execute(sql, params)
        else:
            db.executescript(sql)
        db.commit()
        print(f"  {name}: {time.time() - t0:.1f}s", flush=True)


def write_meta(db: sqlite3.Connection, args: argparse.Namespace, stats: dict[str, int]) -> dict:
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
        "subject_filter": "P31 Q5 (human)",
        "label_languages": ["en", "es"],
        "load_stats": stats,
        "counts": counts,
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    db.commit()
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slice", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/wikidata/people_graph.sqlite"))
    ap.add_argument("--dump-meta", type=Path, default=None)
    args = ap.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".building")
    db = connect(tmp)
    t0 = time.time()
    print(f"loading {args.slice} ...", flush=True)
    stats = load(db, args.slice)
    print(f"loaded: {stats}  ({time.time() - t0:.0f}s)", flush=True)
    print("finalizing ...", flush=True)
    finalize(db)
    meta = write_meta(db, args, stats)
    db.execute("VACUUM")
    db.close()
    if args.out.exists():
        args.out.unlink()
    tmp.rename(args.out)
    print(json.dumps(meta["counts"], indent=2))
    print(f"wrote {args.out} in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
