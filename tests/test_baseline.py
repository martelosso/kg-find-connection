"""Milestone 0 baseline: the existing workshop assets still load and serve.

These tests make no network calls (no OpenRouter, no Wikidata).
"""

import re

import pytest
from rdflib import Graph


@pytest.mark.parametrize("rel_path", ["ontology/ontology.owl.ttl", "data/mock_data.ttl"])
def test_turtle_files_parse(root, rel_path):
    g = Graph()
    g.parse(root / rel_path, format="turtle")
    assert len(g) > 0


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"


def test_ontology_endpoint_returns_turtle(client):
    resp = client.get("/api/ontology")
    assert resp.status_code == 200
    assert resp.mimetype == "text/turtle"
    assert b"@prefix" in resp.data


def test_ask_rejects_empty_body_without_calling_llm(client):
    resp = client.post("/api/ask", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_live_editor_uses_only_local_assets(root):
    html_path = root / "graph-view" / "live-editor.html"
    html = html_path.read_text(encoding="utf-8")

    refs = re.findall(r'<(?:script|link)\b[^>]*(?:src|href)="([^"]+)"', html)
    assert refs, "expected script/link references in live-editor.html"
    for ref in refs:
        assert not re.match(r"^(https?:)?//", ref), f"external asset reference: {ref}"
        assert (html_path.parent / ref).is_file(), f"missing vendored asset: {ref}"
