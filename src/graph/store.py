"""Graph storage behind one read interface used by the pathfinding code.

* ``InMemoryGraph`` — a per-investigation explored subgraph, sandbox overlays and tests.
* ``SqliteGraph``   — the full offline people graph built by scripts/build_people_graph.py.

``complete`` tells the path layer whether the store holds *every* allowlisted edge of the
snapshot (so a finished search may claim ``complete_configured_scope``) or only an explored part.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Protocol

from .model import Edge, Node, NodeKind


class GraphView(Protocol):
    @property
    def complete(self) -> bool: ...

    @property
    def snapshot(self) -> str | None: ...

    def node(self, qid: str) -> Node | None: ...

    def incident(self, qid: str, pids: frozenset[str]) -> list[Edge]:
        """Edges touching ``qid`` (either direction) whose PID is in ``pids``, in a stable order."""
        ...


def _sort_key(edge: Edge) -> tuple[str, str]:
    return (edge.pid, edge.edge_id)


class InMemoryGraph:
    """Small mutable graph. Parallel edges with different predicates are kept separately."""

    def __init__(self, *, complete: bool = False, snapshot: str | None = None) -> None:
        self._complete = complete
        self._snapshot = snapshot
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._adj: dict[str, dict[str, Edge]] = defaultdict(dict)

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def snapshot(self) -> str | None:
        return self._snapshot

    def add_node(self, node: Node) -> None:
        self._nodes[node.qid] = node

    def add_edge(self, edge: Edge) -> bool:
        """Add an edge; returns False if an identical edge (same ID) already exists."""
        if edge.edge_id in self._edges:
            return False
        self._edges[edge.edge_id] = edge
        self._adj[edge.subject][edge.edge_id] = edge
        self._adj[edge.object][edge.edge_id] = edge
        return True

    def add_edges(self, edges: Iterable[Edge]) -> int:
        return sum(self.add_edge(e) for e in edges)

    def node(self, qid: str) -> Node | None:
        node = self._nodes.get(qid)
        degree = len(self._adj.get(qid, ()))
        if node is None:
            return Node(qid=qid, degree=degree) if qid in self._adj else None
        # Explicit degree (e.g. copied from the full graph) wins over the local count.
        return node if node.degree else Node(node.qid, node.label_en, node.label_es, node.kind, degree)

    def incident(self, qid: str, pids: frozenset[str]) -> list[Edge]:
        return sorted((e for e in self._adj.get(qid, {}).values() if e.pid in pids), key=_sort_key)

    @property
    def edges(self) -> list[Edge]:
        return sorted(self._edges.values(), key=lambda e: e.edge_id)

    @property
    def nodes(self) -> list[Node]:
        qids = set(self._nodes) | set(self._adj)
        return [self.node(q) for q in sorted(qids, key=lambda q: int(q[1:]))]  # type: ignore[misc]


class SqliteGraph:
    """Read-only view over the people graph SQLite file (QIDs stored as integers)."""

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        self._db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        meta = {k: json.loads(v) for k, v in self._db.execute("SELECT key, value FROM meta")}
        self.meta = meta
        dump = meta.get("dump") or {}
        self._snapshot = dump.get("dump_date")
        self._source = f"wikidata-truthy:{self._snapshot or 'unknown'}"

    @property
    def complete(self) -> bool:
        return True

    @property
    def snapshot(self) -> str | None:
        return self._snapshot

    def close(self) -> None:
        self._db.close()

    def node(self, qid: str) -> Node | None:
        row = self._db.execute(
            "SELECT label_en, label_es, kind, out_degree + in_degree FROM entity WHERE qid = ?",
            (int(qid[1:]),),
        ).fetchone()
        if row is None:
            return None
        kind: NodeKind = row[2]
        return Node(qid=qid, label_en=row[0], label_es=row[1], kind=kind, degree=row[3])

    def incident(self, qid: str, pids: frozenset[str]) -> list[Edge]:
        if not pids:
            return []
        q = int(qid[1:])
        marks = ",".join("?" * len(pids))
        rows = self._db.execute(
            f"SELECT subject, pid, object FROM edge WHERE subject = ? AND pid IN ({marks}) "
            f"UNION ALL "
            f"SELECT subject, pid, object FROM edge WHERE object = ? AND pid IN ({marks})",
            (q, *sorted(pids), q, *sorted(pids)),
        ).fetchall()
        edges = [Edge(f"Q{s}", p, f"Q{o}", self._source) for s, p, o in rows]
        return sorted(edges, key=_sort_key)


_KINDS: tuple[NodeKind, ...] = ("person", "organization", "position", "other")
_INDEX_FORMAT = 1


class IndexedPeopleGraph(SqliteGraph):
    """``SqliteGraph`` with the adjacency and per-node (kind, degree) held in compact numpy arrays.

    Path searches over the full people graph touch hundreds of thousands of nodes; one SQL query
    per node dominated their run time. This view answers ``incident`` and ``node_profile`` from
    memory (CSR layout, ~10 bytes per edge direction) and reads labels from SQLite only in
    ``node()``, i.e. for the few nodes that end up on a result. It returns exactly what
    ``SqliteGraph`` returns (tests check parity).

    The arrays are built from the SQLite file on first use and saved next to it as
    ``<db>.adjacency.npz`` (rebuilt automatically when the database changes).
    """

    def __init__(self, path: Path | str, *, cache: bool = True) -> None:
        super().__init__(path)
        self._path = Path(path)
        arrays = self._load_cached() if cache else None
        if arrays is None:
            arrays = self._build_arrays()
            if cache:
                self._save_cached(arrays)
        self._qids = arrays["qids"]          # int32, sorted entity QIDs
        self._kind = arrays["kind"]          # int8 index into _KINDS
        self._degree = arrays["degree"]      # int32 out+in degree (all extracted PIDs)
        self._offsets = arrays["offsets"]    # int64, len(qids)+1
        self._nbr = arrays["nbr"]            # int32 neighbour QID per edge direction
        self._code = arrays["code"]          # uint8: pid_index*2 + (1 if qid is the object)
        self._pid_names: list[str] = [str(p) for p in arrays["pids"]]
        self._pid_masks: dict[frozenset[str], object] = {}

    # ---- build / cache ---------------------------------------------------------------------

    def _fingerprint(self) -> str:
        st = self._path.stat()
        return json.dumps({"format": _INDEX_FORMAT, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                           "dump": (self.meta.get("dump") or {}).get("dump_date"),
                           "edges": (self.meta.get("counts") or {}).get("edges")}, sort_keys=True)

    @property
    def cache_path(self) -> Path:
        return self._path.with_name(self._path.name + ".adjacency.npz")

    def _load_cached(self) -> dict | None:
        import numpy as np

        try:
            with np.load(self.cache_path, allow_pickle=False) as npz:
                if str(npz["fingerprint"]) != self._fingerprint():
                    return None
                return {k: npz[k] for k in npz.files}
        except (OSError, KeyError, ValueError):
            return None

    def _save_cached(self, arrays: dict) -> None:
        import numpy as np

        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp.npz")
        try:
            np.savez(tmp, fingerprint=np.array(self._fingerprint()), **arrays)
            tmp.replace(self.cache_path)
        except OSError:  # read-only location: keep the in-memory arrays, skip the cache
            tmp.unlink(missing_ok=True)

    def _build_arrays(self) -> dict:
        import numpy as np

        db = self._db
        kind_case = " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(_KINDS))
        ents = np.array(db.execute(
            f"SELECT qid, CASE kind {kind_case} ELSE {_KINDS.index('other')} END, out_degree + in_degree "
            f"FROM entity ORDER BY qid").fetchall(), dtype=np.int64).reshape(-1, 3)
        qids = ents[:, 0].astype(np.int32)
        pids = sorted(p for (p,) in db.execute("SELECT DISTINCT pid FROM edge"))
        pid_case = " ".join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(pids))
        cur = db.execute(f"SELECT subject, object, CASE pid {pid_case} END FROM edge")
        chunks = []
        while rows := cur.fetchmany(1_000_000):
            chunks.append(np.array(rows, dtype=np.int64).reshape(-1, 3))
        e = np.concatenate(chunks) if chunks else np.zeros((0, 3), dtype=np.int64)
        subj, obj, pcode = e[:, 0], e[:, 1], e[:, 2]
        owner = np.concatenate([subj, obj])
        nbr = np.concatenate([obj, subj]).astype(np.int32)
        code = np.concatenate([pcode * 2, pcode * 2 + 1]).astype(np.uint8)
        order = np.argsort(owner, kind="stable")
        owner_idx = np.searchsorted(qids, owner[order])
        if len(owner_idx) and not np.array_equal(qids[owner_idx], owner[order]):
            raise ValueError("edge endpoint missing from the entity table")
        offsets = np.zeros(len(qids) + 1, dtype=np.int64)
        np.cumsum(np.bincount(owner_idx, minlength=len(qids)), out=offsets[1:])
        return {"qids": qids, "kind": ents[:, 1].astype(np.int8), "degree": ents[:, 2].astype(np.int32),
                "offsets": offsets, "nbr": nbr[order], "code": code[order], "pids": np.array(pids, dtype=str)}

    # ---- queries -------------------------------------------------------------------------------

    def _index(self, qid: str) -> int | None:
        import numpy as np

        try:
            q = int(qid[1:])
        except ValueError:
            return None
        if not 0 <= q <= np.iinfo(np.int32).max:
            return None
        # Same-dtype scalar: a Python int would make numpy convert the whole array on every call.
        i = int(self._qids.searchsorted(np.int32(q)))
        return i if i < len(self._qids) and int(self._qids[i]) == q else None

    def node_profile(self, qid: str) -> tuple[NodeKind, int] | None:
        """(kind, degree) without labels, from memory. None if the QID is not in the graph."""
        i = self._index(qid)
        return None if i is None else (_KINDS[int(self._kind[i])], int(self._degree[i]))

    def _mask(self, pids: frozenset[str]):
        import numpy as np

        mask = self._pid_masks.get(pids)
        if mask is None:
            mask = np.zeros(2 * max(1, len(self._pid_names)), dtype=bool)
            for i, p in enumerate(self._pid_names):
                if p in pids:
                    mask[2 * i] = mask[2 * i + 1] = True
            self._pid_masks[pids] = mask
        return mask

    def incident(self, qid: str, pids: frozenset[str]) -> list[Edge]:
        if not pids:
            return []
        i = self._index(qid)
        if i is None:
            return []
        lo, hi = int(self._offsets[i]), int(self._offsets[i + 1])
        codes = self._code[lo:hi]
        keep = self._mask(pids)[codes]
        names, src = self._pid_names, self._source
        edges = []
        for n, c in zip(self._nbr[lo:hi][keep].tolist(), codes[keep].tolist()):
            pid = names[c >> 1]
            edges.append(Edge(f"Q{n}", pid, qid, src) if c & 1 else Edge(qid, pid, f"Q{n}", src))
        return sorted(edges, key=_sort_key)


def open_people_graph(path: Path | str, *, in_memory: bool = True) -> SqliteGraph:
    """The full people graph for exact path searches: indexed in memory by default."""
    return IndexedPeopleGraph(path) if in_memory else SqliteGraph(path)
