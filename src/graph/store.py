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
