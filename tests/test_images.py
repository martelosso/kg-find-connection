"""Entity pictures (src/wikidata/images.py and GET /api/images). No network: the Wikidata Query
Service is an httpx.MockTransport that records every request."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from src.config import ImagePolicy
from src.wikidata.images import ImageResolver, commons_file, file_page, sparql_query, thumb_url

FILE = "http://commons.wikimedia.org/wiki/Special:FilePath/"


class FakeWdqs:
    """Answers SPARQL POSTs from a {qid: [P18 values]} table, or with scripted failures first."""

    def __init__(self, images, failures=()):
        self.images, self.failures, self.calls = images, list(failures), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["query"][0]
        self.calls.append({"query": query, "ua": request.headers.get("user-agent")})
        if self.failures:
            fail = self.failures.pop(0)
            if isinstance(fail, Exception):
                raise fail
            return httpx.Response(fail[0], headers=fail[1] if len(fail) > 1 else {})
        asked = [tok[3:] for tok in query.split() if tok.startswith("wd:Q")]
        rows = [{"item": {"value": f"http://www.wikidata.org/entity/{q}"},
                 "image": {"value": min(self.images[q])}} for q in asked if self.images.get(q)]
        return httpx.Response(200, json={"results": {"bindings": rows}})


def make(tmp_path, wdqs, policy=None, **kw):
    sleeps = []
    clock = kw.pop("clock", lambda: 0.0)
    resolver = ImageResolver(policy or ImagePolicy(), tmp_path / "images.json",
                             client=httpx.Client(transport=httpx.MockTransport(wdqs)),
                             sleep=sleeps.append, clock=clock, enabled=kw.pop("enabled", True), **kw)
    return resolver, sleeps


def test_file_names_and_urls():
    assert commons_file(FILE + "Ada%20Lovelace%20portrait.jpg") == "Ada Lovelace portrait.jpg"
    assert commons_file("http://example.org/not-a-file") is None
    assert thumb_url("Ada Lovelace.jpg", 160) == "https://commons.wikimedia.org/wiki/Special:FilePath/Ada_Lovelace.jpg?width=160"
    assert file_page("Ada Lovelace.jpg") == "https://commons.wikimedia.org/wiki/File:Ada_Lovelace.jpg"
    q = sparql_query(["Q1", "Q2"])
    assert "VALUES ?item { wd:Q1 wd:Q2 }" in q and "wdt:P18" in q and "MIN(STR(?img))" in q


def test_resolves_caches_and_remembers_no_image(tmp_path):
    wdqs = FakeWdqs({"Q1": [FILE + "B.jpg", FILE + "A.jpg"], "Q2": []})
    resolver, _ = make(tmp_path, wdqs, user_agent="test-agent/1.0 (tests)")
    known, pending = resolver.resolve(["Q1", "Q2", "Q1", "not-a-qid"])
    assert known == {"Q1": "A.jpg", "Q2": None} and pending == []  # first file alphabetically; no image = None
    assert len(wdqs.calls) == 1 and wdqs.calls[0]["ua"] == "test-agent/1.0 (tests)"
    assert resolver.resolve(["Q2", "Q1"])[0] == {"Q1": "A.jpg", "Q2": None}
    assert len(wdqs.calls) == 1  # both answers came from the cache, "no image" included
    # ...and the cache survives a restart
    again, _ = make(tmp_path, FakeWdqs({}))
    assert again.resolve(["Q1", "Q2"]) == ({"Q1": "A.jpg", "Q2": None}, [])
    saved = json.loads((tmp_path / "images.json").read_text(encoding="utf-8"))
    assert saved["entries"]["Q1"]["file"] == "A.jpg" and "fetched_at" in saved["entries"]["Q1"]


def test_batches_follow_the_policy(tmp_path):
    wdqs = FakeWdqs({})
    resolver, _ = make(tmp_path, wdqs, ImagePolicy(batch_size=2))
    known, pending = resolver.resolve(["Q1", "Q2", "Q3", "Q4", "Q5"])
    assert len(wdqs.calls) == 3 and pending == [] and set(known) == {"Q1", "Q2", "Q3", "Q4", "Q5"}


def test_retry_after_is_honoured_with_a_cap(tmp_path):
    wdqs = FakeWdqs({"Q1": [FILE + "A.jpg"]}, failures=[(429, {"Retry-After": "120"}), (503,)])
    resolver, sleeps = make(tmp_path, wdqs, ImagePolicy(max_retries=2, max_retry_after_seconds=30))
    assert resolver.resolve(["Q1"]) == ({"Q1": "A.jpg"}, [])
    assert sleeps == [30, 2.0]  # Retry-After 120 capped at 30, then exponential backoff


def test_a_failed_lookup_is_not_cached_and_backs_off(tmp_path):
    now = [0.0]
    wdqs = FakeWdqs({"Q1": [FILE + "A.jpg"]}, failures=[httpx.ConnectError("offline")] * 3)
    resolver, _ = make(tmp_path, wdqs, ImagePolicy(max_retries=2, offline_backoff_seconds=60), clock=lambda: now[0])
    assert resolver.resolve(["Q1"]) == ({}, ["Q1"])
    assert len(wdqs.calls) == 3
    now[0] = 30.0  # inside the backoff: cache only, no request
    assert resolver.resolve(["Q1"]) == ({}, ["Q1"]) and len(wdqs.calls) == 3
    now[0] = 61.0  # backoff over, the service is back
    assert resolver.resolve(["Q1"]) == ({"Q1": "A.jpg"}, [])


def test_a_client_error_is_not_retried(tmp_path):
    wdqs = FakeWdqs({}, failures=[(400,)])
    resolver, sleeps = make(tmp_path, wdqs)
    assert resolver.resolve(["Q1"]) == ({}, ["Q1"]) and len(wdqs.calls) == 1 and sleeps == []


def test_lookups_off_use_the_cache_only(tmp_path):
    first, _ = make(tmp_path, FakeWdqs({"Q1": [FILE + "A.jpg"]}))
    first.resolve(["Q1"])
    wdqs = FakeWdqs({"Q2": [FILE + "B.jpg"]})
    off, _ = make(tmp_path, wdqs, enabled=False)
    assert off.resolve(["Q1", "Q2"]) == ({"Q1": "A.jpg"}, ["Q2"]) and wdqs.calls == []


def test_the_env_switch_turns_lookups_off(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKIDATA_IMAGES", "0")
    resolver = ImageResolver(ImagePolicy(), tmp_path / "images.json")
    assert resolver.enabled is False
    resolver.close()


def test_a_damaged_cache_is_ignored(tmp_path):
    (tmp_path / "images.json").write_text("{not json", encoding="utf-8")
    resolver, _ = make(tmp_path, FakeWdqs({"Q1": [FILE + "A.jpg"]}))
    assert resolver.resolve(["Q1"]) == ({"Q1": "A.jpg"}, [])


@pytest.fixture
def api(app, tmp_path):
    resolver, _ = make(tmp_path, FakeWdqs({"Q1": [FILE + "Ada%20L.jpg"]}), ImagePolicy(max_per_request=3))
    app.config["IMAGES"] = resolver
    yield app.test_client()
    app.config.pop("IMAGES", None)


def test_the_route_returns_thumbnails_and_nulls(api):
    body = api.get("/api/images?qids=Q1,Q2").get_json()
    assert body["enabled"] is True and body["pending"] == []
    assert body["images"]["Q2"] is None
    assert body["images"]["Q1"] == {"file": "Ada L.jpg",
                                    "thumb": "https://commons.wikimedia.org/wiki/Special:FilePath/Ada_L.jpg?width=160",
                                    "page": "https://commons.wikimedia.org/wiki/File:Ada_L.jpg"}


def test_the_route_limits_the_batch(api):
    res = api.get("/api/images?qids=Q1,Q2,Q3,Q4")
    assert res.status_code == 400 and res.get_json()["error"]["code"] == "invalid_request"
    assert api.get("/api/images?qids=").get_json()["images"] == {}
