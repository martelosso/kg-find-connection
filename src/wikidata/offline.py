"""Offline KnowledgeProviders: the full SQLite people graph, and committed case fixtures.

* ``SqlitePeopleGraphProvider`` — reads data/wikidata/people_graph.sqlite (complete snapshot).
* ``FixtureProvider``           — reads data/fixtures/cases/*.json, recorded from the SQLite
  provider by ``build_case_fixture``. It answers exactly like the SQLite provider for every QID it
  stores and raises ``ProviderError("fixture_gap")`` instead of guessing for anything else.

Fixture format (``six-degrees-case-fixture`` v1)::

    {"format": ..., "version": 1, "case_id": ..., "snapshot": "20261104", "source": "wikidata-truthy:20261104",
     "dump": {...}, "created_at": ..., "properties": [...], "neighbourhood_cap": 500,
     "entities": [[qid, label_en, label_es, kind, degree], ...],
     "neighbourhoods": {"Q7186": {"P26": {"outgoing": {"complete": true, "edges": [[s, pid, o], ...]},
                                           "incoming": {...}}, ...}}}

Each stored list is the provider ordering's PREFIX of length <= cap, so any expansion with
``max_edges <= cap`` is reproduced exactly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from ..graph.model import Edge, Node, is_qid
from .provider import NeighbourhoodProvider, PersonMatch, ProviderError, RowList, neighbour_key

FIXTURE_FORMAT = "six-degrees-case-fixture"
OneDirection = Literal["outgoing", "incoming"]


def _qint(qid: str) -> int:
    return int(qid[1:])


class SqlitePeopleGraphProvider(NeighbourhoodProvider):
    name = "people-graph-sqlite"

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        self.path = path
        self._db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self.meta = {k: json.loads(v) for k, v in self._db.execute("SELECT key, value FROM meta")}
        self.dump = self.meta.get("dump") or {}
        self._snapshot = self.dump.get("dump_date")
        self.source = f"wikidata-truthy:{self._snapshot or 'unknown'}"
        self._node_cache: dict[str, Node | None] = {}

    @property
    def snapshot(self) -> str | None:
        return self._snapshot

    def close(self) -> None:
        self._db.close()

    def _node(self, qid: str) -> Node | None:
        if qid in self._node_cache:
            return self._node_cache[qid]
        row = self._db.execute(
            "SELECT label_en, label_es, kind, out_degree + in_degree FROM entity WHERE qid = ?", (_qint(qid),)
        ).fetchone()
        node = None if row is None else Node(qid, row[0], row[1], row[2], row[3])
        if len(self._node_cache) > 200_000:
            self._node_cache.clear()
        self._node_cache[qid] = node
        return node

    def _rows(self, qid: str, pid: str, direction: OneDirection) -> RowList:
        here, there = ("subject", "object") if direction == "outgoing" else ("object", "subject")
        rows = self._db.execute(
            f"SELECT e.subject, e.object, n.label_en, n.label_es, n.kind, n.out_degree + n.in_degree "
            f"FROM edge e JOIN entity n ON n.qid = e.{there} WHERE e.{here} = ? AND e.pid = ?",
            (_qint(qid), pid),
        ).fetchall()
        items = []
        for s, o, l_en, l_es, kind, deg in rows:
            edge = Edge(f"Q{s}", pid, f"Q{o}", self.source)
            other = edge.other(qid)
            node = Node(other, l_en, l_es, kind, deg)
            self._node_cache.setdefault(other, node)
            items.append((edge, node))
        items.sort(key=lambda en: neighbour_key(*en))
        return RowList(tuple(items), True)

    def search_people(self, text: str, limit: int = 8) -> list[PersonMatch]:
        text = " ".join(text.split())
        limit = max(1, min(limit, 25))
        if not text:
            return []
        if is_qid(text.upper()):
            node = self._node(text.upper())
            if node is None or node.kind != "person":
                return []
            return [PersonMatch(**_entity_fields(node), matched_label=node.label(), matched_lang="qid")]
        hits: dict[str, PersonMatch] = {}
        hi = text + "\U0010ffff"
        for lang in ("en", "es"):
            col = f"label_{lang}"
            rows = self._db.execute(
                f"SELECT qid, label_en, label_es, kind, out_degree + in_degree AS deg FROM entity "
                f"WHERE is_human AND {col} >= ? COLLATE NOCASE AND {col} < ? COLLATE NOCASE "
                f"ORDER BY deg DESC, qid LIMIT ?",
                (text, hi, limit * 4),
            ).fetchall()
            for q, l_en, l_es, kind, deg in rows:
                qid = f"Q{q}"
                node = Node(qid, l_en, l_es, kind, deg)
                if qid not in hits:
                    hits[qid] = PersonMatch(**_entity_fields(node), matched_label=l_en if lang == "en" else l_es,
                                            matched_lang=lang)
        ranked = sorted(hits.values(), key=lambda m: (m.matched_label.casefold() != text.casefold(),
                                                       -m.degree, int(m.qid[1:])))
        return ranked[:limit]


def _entity_fields(node: Node) -> dict:
    return dict(qid=node.qid, label_en=node.label_en, label_es=node.label_es,
                kind=node.kind, degree=node.degree, url=node.url)


class FixtureProvider(NeighbourhoodProvider):
    name = "case-fixtures"

    def __init__(self, fixtures: Iterable[dict]) -> None:
        fixtures = list(fixtures)
        self._nodes: dict[str, Node] = {}
        self._hoods: dict[str, dict[str, dict[str, RowList]]] = {}
        self.cases: list[str] = []
        snapshots, sources = set(), set()
        for fx in fixtures:
            if fx.get("format") != FIXTURE_FORMAT or fx.get("version") != 1:
                raise ValueError(f"not a v1 case fixture: {fx.get('case_id')!r}")
            self.cases.append(fx["case_id"])
            snapshots.add(fx["snapshot"])
            sources.add(fx["source"])
            for qid, l_en, l_es, kind, deg in fx["entities"]:
                self._nodes[qid] = Node(qid, l_en, l_es, kind, deg)
        if len(snapshots) > 1:
            raise ValueError(f"fixtures mix snapshots {sorted(snapshots)}")
        self._snapshot = snapshots.pop() if snapshots else None
        self.source = sources.pop() if len(sources) == 1 else "mixed"
        for fx in fixtures:
            self._load_hoods(fx)

    @classmethod
    def from_files(cls, paths: Iterable[Path]) -> "FixtureProvider":
        data = [json.loads(Path(p).read_text(encoding="utf-8")) for p in sorted(paths)]
        return cls(data)

    @classmethod
    def from_dir(cls, directory: Path | str) -> "FixtureProvider":
        return cls.from_files(Path(directory).glob("*.json"))

    def _load_hoods(self, fx: dict) -> None:
        for qid, by_pid in fx["neighbourhoods"].items():
            for pid, by_dir in by_pid.items():
                for direction, stored in by_dir.items():
                    items = []
                    for s, p, o in stored["edges"]:
                        edge = Edge(s, p, o, fx["source"])
                        items.append((edge, self._nodes[edge.other(qid)]))
                    self._hoods.setdefault(qid, {}).setdefault(pid, {})[direction] = RowList(
                        tuple(items), bool(stored["complete"]))

    @property
    def snapshot(self) -> str | None:
        return self._snapshot

    def covers(self, qid: str) -> bool:
        return qid in self._hoods

    def _node(self, qid: str) -> Node | None:
        return self._nodes.get(qid)

    def _rows(self, qid: str, pid: str, direction: OneDirection) -> RowList:
        hood = self._hoods.get(qid)
        if hood is None:
            raise ProviderError("fixture_gap", f"{qid} was not recorded in the case fixtures")
        by_dir = hood.get(pid)
        if by_dir is None or direction not in by_dir:
            raise ProviderError("fixture_gap", f"{qid} {pid} {direction} was not recorded")
        return by_dir[direction]

    def search_people(self, text: str, limit: int = 8) -> list[PersonMatch]:
        needle = " ".join(text.split()).casefold()
        if not needle:
            return []
        hits = []
        for node in self._nodes.values():
            if node.kind != "person":
                continue
            if needle.upper() == node.qid:
                hits.append(PersonMatch(**_entity_fields(node), matched_label=node.label(), matched_lang="qid"))
                continue
            for lang, label in (("en", node.label_en), ("es", node.label_es)):
                if label and label.casefold().startswith(needle):
                    hits.append(PersonMatch(**_entity_fields(node), matched_label=label, matched_lang=lang))
                    break
        hits.sort(key=lambda m: (m.matched_label.casefold() != needle, -m.degree, int(m.qid[1:])))
        return hits[: max(1, min(limit, 25))]


def build_case_fixture(
    provider: SqlitePeopleGraphProvider,
    case_id: str,
    qids: Iterable[str],
    properties: Iterable[str],
    *,
    cap: int = 500,
) -> dict:
    """Record the stored-neighbourhood prefixes of ``qids`` from the full people graph."""
    pids = sorted(set(properties), key=lambda p: int(p[1:]))
    hoods: dict[str, dict] = {}
    entities: dict[str, Node] = {}
    for qid in sorted(set(qids), key=_qint):
        node = provider._node(qid)
        if node is None:
            raise ProviderError("unknown_entity", qid)
        entities[qid] = node
        for pid in pids:
            for direction in ("outgoing", "incoming"):
                rl = provider._rows(qid, pid, direction)  # type: ignore[arg-type]
                kept = rl.items[:cap]
                for _, n in kept:
                    entities[n.qid] = n
                hoods.setdefault(qid, {}).setdefault(pid, {})[direction] = {
                    "complete": len(rl.items) <= cap,
                    "edges": [[e.subject, e.pid, e.object] for e, _ in kept],
                }
    return {
        "format": FIXTURE_FORMAT,
        "version": 1,
        "case_id": case_id,
        "snapshot": provider.snapshot,
        "source": provider.source,
        "dump": {k: provider.dump.get(k) for k in ("file", "source", "dump_date", "md5", "size") if k in provider.dump},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "properties": pids,
        "neighbourhood_cap": cap,
        "entities": [[n.qid, n.label_en, n.label_es, n.kind, n.degree]
                     for n in sorted(entities.values(), key=lambda n: _qint(n.qid))],
        "neighbourhoods": hoods,
    }
