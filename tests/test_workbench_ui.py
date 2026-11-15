"""Workbench static assets: served offline, modules resolvable, no HTML injection of Wikidata text."""

import re

import pytest

UI = "src/ui"


def test_workbench_is_served_at_root_with_local_assets(client, root):
    html = client.get("/").get_data(as_text=True)
    assert "<title>Six Degrees of Knowledge</title>" in html
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs and all(r.startswith("/") for r in refs), refs  # no CDN / external URLs
    for ref in refs:
        res = client.get(ref)
        assert res.status_code == 200, ref
        if ref.endswith(".js"):
            assert res.mimetype == "text/javascript", ref  # ES modules need a JS MIME type
        if ref.endswith(".css"):
            assert res.mimetype == "text/css", ref


def test_workbench_font_is_bundled_and_served_as_a_font(client, root):
    css = (root / UI / "css" / "workbench.css").read_text(encoding="utf-8")
    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert urls and all(u.startswith("/vendor/fonts/") for u in urls), urls  # offline: no font CDN
    for url in set(urls):
        res = client.get(url)
        assert res.status_code == 200, url
        assert res.mimetype == "font/woff2", url
    assert (root / UI / "vendor" / "fonts" / "OFL.txt").is_file()


def test_every_module_import_resolves(root):
    js_dir = root / UI / "js"
    for path in js_dir.glob("*.js"):
        for target in re.findall(r'from "(\./[^"]+)"', path.read_text(encoding="utf-8")):
            assert (js_dir / target).is_file(), f"{path.name} imports missing {target}"


def test_no_innerhtml_anywhere_in_the_workbench(root):
    for path in [root / UI / "index.html", *(root / UI / "js").glob("*.js")]:
        assert "innerHTML" not in path.read_text(encoding="utf-8"), path.name


def test_legacy_and_preview_routes(client):
    assert client.get("/legacy").status_code == 200
    res = client.get("/preview?source=Q1&target=Q2")
    assert res.status_code == 302 and res.headers["Location"].endswith("/?source=Q1&target=Q2")


def test_vendored_licenses_are_kept(root):
    assert (root / UI / "vendor" / "codemirror" / "LICENSE").is_file()
    head = (root / UI / "vendor" / "vis-network.min.js").read_text(encoding="utf-8")[:2000]
    assert "vis-network" in head and ("Apache" in head or "MIT" in head)


@pytest.mark.parametrize("page", ["live-editor.html", "visualize.html"])
def test_legacy_graph_view_pages_still_find_their_vendored_scripts(root, page):
    html_path = root / "graph-view" / page
    refs = [r for r in re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', html_path.read_text(encoding="utf-8"))]
    assert refs
    for ref in refs:
        assert not ref.startswith("http"), ref
        assert (html_path.parent / ref).resolve().is_file(), f"{page}: missing {ref}"
