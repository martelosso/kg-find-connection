"""Entity pictures: each entity's Wikidata image (P18), looked up live and cached on disk.

The people graph has no images (P18 was never extracted), so the workbench asks this resolver for
the entities on screen. It queries the Wikidata Query Service in batches with one SPARQL SELECT:

    SELECT ?item (MIN(STR(?img)) AS ?image) WHERE { VALUES ?item { wd:Q… } ?item wdt:P18 ?img } GROUP BY ?item

``wdt:P18`` holds only best-rank statements; when an entity has several, the alphabetically first
file is taken, so the answer is deterministic. Every answer is cached with its timestamp, "no image"
included. A failed lookup (no network, timeout, 5xx after retries) is **not** cached: those QIDs
come back as ``pending`` and the resolver serves the cache only for ``offline_backoff_seconds``
before trying again. Rules followed (CLAUDE.md "Wikidata access"): descriptive User-Agent, timeout,
one request at a time, exponential backoff that honours ``429 Retry-After`` with a cap, bounded
batches of QIDs that the workbench actually shows, never a crawl.

Settings: ``images:`` in config/search.yaml. ``WIKIDATA_IMAGES=0`` turns lookups off (the cache is
still used); ``WIKIDATA_USER_AGENT`` overrides the User-Agent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote

import httpx

from ..config import ImagePolicy
from ..graph.model import is_qid

SPARQL_URL = "https://query.wikidata.org/sparql"
FILEPATH_PREFIX = "Special:FilePath/"
DEFAULT_USER_AGENT = "knowledge-graph-workshop/0.1 (workbench entity images)"
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "wikidata" / "cache" / "images.json"
RETRY_STATUS = {429, 500, 502, 503, 504}


def commons_file(value: str) -> str | None:
    """The Commons file name in a P18 value (``…/Special:FilePath/Foo%20bar.jpg`` → ``Foo bar.jpg``)."""
    i = value.find(FILEPATH_PREFIX)
    if i < 0:
        return None
    name = unquote(value[i + len(FILEPATH_PREFIX):]).strip()
    return name or None


def thumb_url(file: str, width: int) -> str:
    """A Commons thumbnail of ``file`` (Special:FilePath redirects to upload.wikimedia.org)."""
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(file.replace(' ', '_'))}?width={width}"


def file_page(file: str) -> str:
    """The file's description page on Commons (author and license)."""
    return f"https://commons.wikimedia.org/wiki/File:{quote(file.replace(' ', '_'))}"


def sparql_query(qids: list[str]) -> str:
    values = " ".join(f"wd:{q}" for q in qids)
    return ("SELECT ?item (MIN(STR(?img)) AS ?image) WHERE { "
            f"VALUES ?item {{ {values} }} ?item wdt:P18 ?img . }} GROUP BY ?item")


class LookupFailed(Exception):
    """The query service could not answer (after retries)."""


class ImageResolver:
    """QID → Commons file name (or None for "no image"), from a disk cache backed by live lookups."""

    def __init__(self, policy: ImagePolicy, cache_path: Path | str = DEFAULT_CACHE, *,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, enabled: bool | None = None,
                 user_agent: str | None = None) -> None:
        self.policy = policy
        self.cache_path = Path(cache_path)
        if enabled is None:
            enabled = policy.enabled and os.environ.get("WIKIDATA_IMAGES", "1").strip().lower() not in ("0", "false", "no", "off")
        self.enabled = enabled
        ua = user_agent or os.environ.get("WIKIDATA_USER_AGENT") or DEFAULT_USER_AGENT
        self._client = client or httpx.Client(timeout=policy.timeout_seconds, follow_redirects=True)
        self._headers = {"User-Agent": ua, "Accept": "application/sparql-results+json"}
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()   # one lookup at a time, and the cache file written by one thread
        self._offline_until = 0.0
        self._cache: dict[str, dict] = self._load()

    # ---- cache -----------------------------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            entries = data.get("entries", {}) if isinstance(data, dict) else {}
            return {q: e for q, e in entries.items() if is_qid(q) and isinstance(e, dict) and "file" in e}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "source": SPARQL_URL, "entries": self._cache},
                                  ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.cache_path)

    # ---- lookups ---------------------------------------------------------------------------------
    def resolve(self, qids: list[str]) -> tuple[dict[str, str | None], list[str]]:
        """Return ``({qid: file or None}, pending)``. ``pending``: not known yet and not looked up
        now (lookups off, the service unreachable, or inside the offline backoff)."""
        wanted = list(dict.fromkeys(q for q in qids if is_qid(q)))[: self.policy.max_per_request]
        with self._lock:
            known = {q: self._cache[q]["file"] for q in wanted if q in self._cache}
            missing = [q for q in wanted if q not in known]
            if not missing or not self.enabled or self._clock() < self._offline_until:
                return known, missing
            pending: list[str] = []
            changed = False
            for i in range(0, len(missing), self.policy.batch_size):
                batch = missing[i: i + self.policy.batch_size]
                if pending:           # an earlier batch failed: don't hammer the service
                    pending += batch
                    continue
                try:
                    found = self._query(batch)
                except LookupFailed:
                    self._offline_until = self._clock() + self.policy.offline_backoff_seconds
                    pending += batch
                    continue
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for q in batch:
                    self._cache[q] = {"file": found.get(q), "fetched_at": stamp}
                    known[q] = found.get(q)
                changed = True
            if changed:
                try:
                    self._save()
                except OSError:
                    pass  # an unwritable cache only costs repeated lookups
            return known, pending

    def _query(self, qids: list[str]) -> dict[str, str]:
        delay = 1.0
        for attempt in range(self.policy.max_retries + 1):
            last = attempt == self.policy.max_retries
            try:
                res = self._client.post(SPARQL_URL, data={"query": sparql_query(qids)}, headers=self._headers,
                                        timeout=self.policy.timeout_seconds)
            except httpx.HTTPError as exc:
                if last:
                    raise LookupFailed(str(exc)) from exc
                self._sleep(delay)
                delay *= 2
                continue
            if res.status_code == 200:
                return self._parse(res)
            if res.status_code not in RETRY_STATUS or last:
                raise LookupFailed(f"HTTP {res.status_code}")
            wait = delay
            retry_after = res.headers.get("Retry-After", "")
            if retry_after.strip().isdigit():
                wait = float(retry_after)
            self._sleep(min(wait, self.policy.max_retry_after_seconds))
            delay *= 2
        raise LookupFailed("no attempts")  # unreachable: the loop returns or raises

    @staticmethod
    def _parse(res: httpx.Response) -> dict[str, str]:
        try:
            rows = res.json()["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LookupFailed("unexpected SPARQL response") from exc
        out: dict[str, str] = {}
        for row in rows:
            qid = row.get("item", {}).get("value", "").rsplit("/", 1)[-1]
            file = commons_file(row.get("image", {}).get("value", ""))
            if is_qid(qid) and file:
                out[qid] = file
        return out

    def describe(self, qids: list[str]) -> dict:
        """The /api/images payload: per QID ``{file, thumb, page}`` or ``None``, plus ``pending``."""
        known, pending = self.resolve(qids)
        w = self.policy.thumb_width
        return {
            "enabled": self.enabled,
            "images": {q: ({"file": f, "thumb": thumb_url(f, w), "page": file_page(f)} if f else None)
                       for q, f in known.items()},
            "pending": pending,
        }

    def close(self) -> None:
        self._client.close()
