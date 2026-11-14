"""Flask app: Six Degrees investigation API (plan §7) plus the legacy retail assistant routes.

Usage:
    uv run python -m src.server

Then open http://localhost:5000. The legacy /api/ask route requires OPENROUTER_API_KEY in .env;
the investigation API runs offline (people graph or case fixtures; replay needs neither).
"""

from __future__ import annotations

from pathlib import Path

import json

import mimetypes

from flask import Flask, Response, current_app, jsonify, redirect, request, send_from_directory, stream_with_context
from openai import OpenAI

from .investigations.service import InvestigationService, ServiceError, provider_from_env
from .ontology_agent import (
    DEFAULT_DATA_PATH,
    DEFAULT_MODEL,
    ask,
    build_client,
    build_system_prompt,
)

UI_DIR = Path(__file__).parent / "ui"
# ES modules need a JavaScript MIME type; the Windows registry sometimes maps .js to text/plain.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

app = Flask(__name__, static_folder=str(UI_DIR), static_url_path="")
_system_prompt = build_system_prompt()
_client: OpenAI | None = None


@app.get("/")
def index():
    """Six Degrees of Knowledge workbench."""
    return send_from_directory(UI_DIR, "index.html")


@app.get("/legacy")
def legacy_retail():
    """Original retail (Smarket) assistant UI, kept until it is retired."""
    return send_from_directory(UI_DIR / "legacy", "retail.html")


@app.get("/preview")
def preview():
    """The M4 live preview grew into the workbench."""
    qs = request.query_string.decode()
    return redirect("/" + ("?" + qs if qs else ""))


@app.get("/api/ontology")
def api_ontology():
    from .ontology_agent import DEFAULT_ONTOLOGY_PATH
    return DEFAULT_ONTOLOGY_PATH.read_text(), 200, {"Content-Type": "text/turtle; charset=utf-8"}


@app.post("/api/ask")
def api_ask():
    global _client

    body     = request.get_json(silent=True) or {}
    messages = body.get("messages", [])

    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages array is required"}), 400

    last = messages[-1]
    if not isinstance(last, dict) or not last.get("content", "").strip():
        return jsonify({"error": "last message must have non-empty content"}), 400

    if _client is None:
        try:
            _client = build_client()
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 503

    result = ask(messages, _system_prompt, _client, model=DEFAULT_MODEL, data_path=DEFAULT_DATA_PATH)
    return jsonify(result)


# ---- Six Degrees investigation API ------------------------------------------------------------


def _service() -> InvestigationService:
    svc = current_app.config.get("INVESTIGATIONS")
    if svc is None:
        svc = InvestigationService(provider_from_env())
        current_app.config["INVESTIGATIONS"] = svc
    return svc


@app.errorhandler(ServiceError)
def _service_error(error: ServiceError):
    return jsonify({"error": {"code": error.code, "message": error.message}}), error.status


@app.get("/api/health")
def api_health():
    return jsonify(_service().health())


@app.get("/api/people/search")
def api_people_search():
    svc = _service()
    q = request.args.get("q", "").strip()
    if len(q) > 200:
        return jsonify({"error": {"code": "invalid_request", "message": "query too long"}}), 400
    if svc.provider is None:
        return jsonify({"error": {"code": "provider_unavailable", "message": "no people graph installed"}}), 503
    limit = request.args.get("limit", default=8, type=int)
    matches = svc.provider.search_people(q, limit=limit) if len(q) >= 2 else []
    return jsonify({"query": q, "matches": [m.model_dump() for m in matches]})


@app.get("/api/demo-cases")
def api_demo_cases():
    return jsonify(_service().demo_cases.model_dump())


@app.post("/api/investigations")
def api_start_investigation():
    state = _service().start(request.get_json(silent=True) or {})
    return jsonify(state.summary()), 202


@app.get("/api/investigations/<inv_id>")
def api_investigation(inv_id: str):
    return jsonify(_service().get(inv_id).summary())


@app.post("/api/investigations/<inv_id>/cancel")
def api_cancel(inv_id: str):
    return jsonify(_service().cancel(inv_id).summary())


@app.get("/api/investigations/<inv_id>/events")
def api_events(inv_id: str):
    """Server-Sent Events. Resume with ``Last-Event-ID`` or ``?after=<event_id>``."""
    state = _service().get(inv_id)
    after = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
    try:
        last = max(0, int(after))
    except ValueError:
        last = 0

    def stream():
        nonlocal last
        while True:
            batch = state.events.wait_after(last, timeout=15)
            if not batch:
                yield ": keep-alive\n\n"
                continue
            for e in batch:
                last = e.event_id
                data = json.dumps(e.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {e.event_id}\nevent: {e.type}\ndata: {data}\n\n"
            if state.events.finished and last >= len(state.events.events):
                return

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/investigations/<inv_id>/graph")
def api_graph(inv_id: str):
    from .investigations.baseline import edge_labels
    from .investigations.events import edge_payload, node_payload

    svc = _service()
    g = svc.get(inv_id).graph
    return jsonify({"nodes": [node_payload(n) for n in g.nodes],
                    "edges": [edge_payload(e, **edge_labels(svc.policy, e.pid)) for e in g.edges]})


@app.get("/api/investigations/<inv_id>/paths")
def api_paths(inv_id: str):
    state = _service().get(inv_id)
    cmp = state.comparison
    return jsonify({"status": state.status, "comparison": cmp.model_dump(mode="json") if cmp else None})


@app.route("/api/investigations/<inv_id>/turtle", methods=["GET", "POST"])
def api_turtle(inv_id: str):
    """Compact Turtle of the explored graph (+ sandbox section).

    GET: all explored edges, or ``?scope=selected&edge_ids=a,b``. POST ``{"edge_ids": [...]}`` for
    long selections (e.g. the visible subgraph). ``?format=text`` returns text/turtle.
    """
    edge_ids = None
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if body.get("edge_ids") is not None:
            if not isinstance(body["edge_ids"], list) or len(body["edge_ids"]) > 20_000:
                return jsonify({"error": {"code": "invalid_request", "message": "edge_ids must be a list"}}), 400
            edge_ids = [str(x) for x in body["edge_ids"]]
    elif request.args.get("scope") == "selected":
        edge_ids = list(filter(None, request.args.get("edge_ids", "").split(",")))
    proj = _service().turtle(inv_id, edge_ids)
    if request.args.get("format") == "text":
        return proj.text, 200, {"Content-Type": "text/turtle; charset=utf-8"}
    return jsonify(proj.model_dump())


@app.route("/api/investigations/<inv_id>/record", methods=["GET", "POST"])
def api_record(inv_id: str):
    """Save a finished live run as a demo replay. GET: a suggested case id and title. POST
    ``{"case_id": "...", "title": "..."}``: verify, write fixture + trace, append to demo_cases.yaml."""
    svc = _service()
    if request.method == "GET":
        return jsonify(svc.suggested_case(inv_id))
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("case_id"), str):
        return jsonify({"error": {"code": "invalid_request", "message": "case_id is required"}}), 400
    title = body.get("title") if isinstance(body.get("title"), str) else ""
    return jsonify(svc.record(inv_id, body["case_id"].strip(), title[:200])), 201


@app.route("/api/investigations/<inv_id>/overlay", methods=["GET", "POST", "DELETE"])
def api_overlay(inv_id: str):
    """Sandbox overlay. POST ``{"text": "<turtle>"}``: sbx: statements become hypothetical edges."""
    svc = _service()
    if request.method == "GET":
        return jsonify(svc.overlay(inv_id))
    if request.method == "DELETE":
        return jsonify(svc.clear_overlay(inv_id))
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("text"), str):
        return jsonify({"error": {"code": "invalid_request", "message": "text (Turtle) is required"}}), 400
    shown = body.get("shown_edge_ids")
    if shown is not None and not isinstance(shown, list):
        return jsonify({"error": {"code": "invalid_request", "message": "shown_edge_ids must be a list"}}), 400
    return jsonify(svc.set_overlay(inv_id, body["text"], [str(x) for x in shown] if shown is not None else None))


def main() -> None:
    app.run(port=5000, debug=False)


if __name__ == "__main__":
    main()
